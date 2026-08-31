"""On-disk shadow catalog. Claims only. Never the 44k RAM world index.

catalog.sqlite is process-local on the Fly /data volume (or /tmp fallback).
It is not an HTTP download, not under static/, and not in OpenAPI.
Physically separate from 402signal_observed (history.py).
Ingest is page → normalize → upsert → commit → discard. No full-world list.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone

from live402 import payment, probe

DEFAULT_DB = "/tmp/live402-catalog.sqlite"
VOLUME_DB = "/data/catalog.sqlite"

SOURCE_CDP = "cdp"
SOURCE_PAYAI = "payai"
SOURCE_GOPLAUSIBLE = "goplausible"
SOURCES = (SOURCE_CDP, SOURCE_PAYAI, SOURCE_GOPLAUSIBLE)
RAIL_TO_SOURCE = {"base": SOURCE_CDP, "solana": SOURCE_PAYAI, "algorand": SOURCE_GOPLAUSIBLE}
SOURCE_TO_RAIL = {SOURCE_CDP: "base", SOURCE_PAYAI: "solana", SOURCE_GOPLAUSIBLE: "algorand"}

STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"

EVENT_ADDED = "resource_added"
EVENT_RETIRED = "resource_retired"
EVENT_RAIL_ADDED = "rail_added"
EVENT_RAIL_REMOVED = "rail_removed"
EVENT_PRICE = "price_changed"
EVENT_PAYTO = "payTo_changed"
EVENT_SCHEMA = "schema_changed"
EVENT_CAP = 5_000

FTS_LIMIT = 20
HOT_ELIGIBLE_S = 3_600
WARM_ELIGIBLE_S = 86_400
# Deterministic information-value refresh. Higher first. No ML.
REFRESH_REASONS = (
    "recent_search",
    "recent_route",
    "source_disagreement",
    "price_change",
    "payto_change",
    "schema_change",
    "failed_probe",
    "stale_observation",
    "high_demand_capability",
)
QUEUE_SCAN = 20
HIGH_DEMAND_MIN = 2

_FTS_TOKEN = re.compile(r"[A-Za-z0-9_]{2,}")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None
_fts_ok: bool | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_url TEXT NOT NULL UNIQUE,
    service_name TEXT,
    description TEXT,
    capability TEXT,
    method TEXT,
    tags TEXT,
    input_schema_present INTEGER NOT NULL DEFAULT 0,
    output_schema_present INTEGER NOT NULL DEFAULT 0,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    last_fetched INTEGER,
    last_verified INTEGER,
    last_searched INTEGER,
    last_routed INTEGER,
    last_probe_ok INTEGER,
    row_hash TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    retired_at INTEGER,
    reappeared_at INTEGER
);
CREATE INDEX IF NOT EXISTS resources_status_seen ON resources(status, last_seen);
CREATE INDEX IF NOT EXISTS resources_heat ON resources(last_searched, last_routed, last_fetched);

CREATE TABLE IF NOT EXISTS resource_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_resource_id TEXT,
    source_generation INTEGER,
    source_last_seen INTEGER,
    UNIQUE(resource_id, source)
);
CREATE INDEX IF NOT EXISTS resource_sources_source_gen ON resource_sources(source, source_generation);

CREATE TABLE IF NOT EXISTS accept_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    rail TEXT,
    network TEXT,
    asset TEXT,
    amount_atomic TEXT,
    payTo TEXT,
    facilitator TEXT
);
CREATE INDEX IF NOT EXISTS accept_claims_resource ON accept_claims(resource_id, source);

CREATE TABLE IF NOT EXISTS source_state (
    source TEXT PRIMARY KEY,
    generation INTEGER NOT NULL DEFAULT 0,
    cursor INTEGER NOT NULL DEFAULT 0,
    upstream_total INTEGER,
    sweep_started_at INTEGER,
    last_complete_sweep_at INTEGER
);

CREATE TABLE IF NOT EXISTS claim_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER,
    canonical_url TEXT,
    event TEXT NOT NULL,
    source TEXT,
    detail TEXT,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS claim_events_ts ON claim_events(ts);
CREATE INDEX IF NOT EXISTS claim_events_url ON claim_events(canonical_url, ts);

CREATE TABLE IF NOT EXISTS finalist_contracts (
    canonical_url TEXT PRIMARY KEY,
    method TEXT,
    content_type TEXT,
    tool_name TEXT,
    type TEXT,
    input_schema BLOB,
    output_schema BLOB,
    schema_bytes INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    fetched_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS finalist_contracts_exp ON finalist_contracts(expires_at);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS resource_fts USING fts5(
    service_name,
    description,
    tags,
    capability,
    url,
    tokenize='unicode61 remove_diacritics 2'
);
"""


def db_path() -> str:
    raw = (os.environ.get("LIVE402_CATALOG_DB") or "").strip()
    if raw:
        return raw
    try:
        if os.path.isdir("/data") and os.access("/data", os.W_OK):
            return VOLUME_DB
    except Exception:
        pass
    return DEFAULT_DB


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        n = int(raw) if raw else default
    except ValueError:
        n = default
    return min(max(n, lo), hi)


def hot_refresh_s() -> int:
    """HOT recency refresh. 5–15 min. Default 10 min."""
    return _env_int("LIVE402_HOT_REFRESH_S", 600, 300, 900)


def warm_refresh_s() -> int:
    """WARM refresh. 1–3 h. Default 2 h."""
    return _env_int("LIVE402_WARM_REFRESH_S", 7200, 3600, 10800)


def cold_sweep_s() -> int:
    """COLD rolling sweep cadence. 12–24 h. Default 18 h."""
    return _env_int("LIVE402_COLD_SWEEP_S", 18 * 3600, 12 * 3600, 24 * 3600)


def trickle_sleep_s() -> int:
    return _env_int("LIVE402_TRICKLE_SLEEP_S", 2, 1, 30)


def source_for_rail(rail: str | None) -> str:
    return RAIL_TO_SOURCE.get((rail or "").strip().lower(), SOURCE_CDP)


def rail_for_source(source: str | None) -> str:
    return SOURCE_TO_RAIL.get((source or "").strip().lower(), "base")


def _now() -> int:
    return int(time.time())


def _text(val) -> str | None:
    if val is None:
        return None
    text = str(val).strip()
    return text or None


def _as_int(val, default=None):
    if val is None or val is False:
        return default
    if isinstance(val, bool):
        return int(val)
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _iso_ts(ts) -> str | None:
    try:
        n = int(ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(n, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _chmod_db_files(path: str) -> None:
    for p in (path, path + "-wal", path + "-shm"):
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first catalog.sqlite. Never drops."""
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(resources)").fetchall()}
    except sqlite3.Error:
        return
    if "last_probe_ok" not in cols:
        try:
            conn.execute("ALTER TABLE resources ADD COLUMN last_probe_ok INTEGER")
        except sqlite3.OperationalError:
            pass


def refresh_priority_order() -> tuple[str, ...]:
    """Documented queue order. First reason wins. Same tuple in README."""
    return REFRESH_REASONS


def _connect() -> sqlite3.Connection:
    global _conn, _conn_path, _fts_ok
    path = db_path()
    if _conn is not None and _conn_path == path:
        return _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None
        _conn_path = None
        _fts_ok = None
    conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    _ensure_columns(conn)
    if _fts_ok is None:
        try:
            conn.executescript(_FTS_SCHEMA)
            _fts_ok = True
        except sqlite3.OperationalError:
            _fts_ok = False
    conn.commit()
    _conn = conn
    _conn_path = path
    _chmod_db_files(path)
    return conn


def reset() -> None:
    """Delete the catalog DB (tests). Never the observation DB."""
    global _conn, _conn_path, _fts_ok
    with _lock:
        path = _conn_path or db_path()
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
            _conn_path = None
            _fts_ok = None
        for p in (path, path + "-wal", path + "-shm"):
            try:
                os.remove(p)
            except OSError:
                pass


def fts_available() -> bool:
    with _lock:
        _connect()
        return bool(_fts_ok)


def _claims_from_item(item: dict, source: str) -> list[dict]:
    out: list[dict] = []
    raw = item.get("accepts") if isinstance(item, dict) else None
    if not isinstance(raw, list):
        return out
    fallback = item.get("_rail") or rail_for_source(source)
    seen: set[tuple] = set()
    for acc in raw:
        if not isinstance(acc, dict):
            continue
        opt = payment.payment_option_from_accept(acc, fallback)
        rail = (opt or {}).get("rail") or payment.rail_of_network(acc.get("network") or fallback) or fallback
        asset = (opt or {}).get("asset") or acc.get("asset") or acc.get("currency")
        amount = (opt or {}).get("amount_atomic")
        if amount is None:
            amount = acc.get("amount")
            if amount is None:
                amount = acc.get("maxAmountRequired")
        pay_to = acc.get("payTo")
        fac = (opt or {}).get("facilitator")
        if not fac:
            extra = acc.get("extra") if isinstance(acc.get("extra"), dict) else {}
            raw_fac = extra.get("facilitator")
            if isinstance(raw_fac, str) and raw_fac.strip().startswith("https://"):
                fac = raw_fac.strip()
            elif isinstance(raw_fac, dict):
                cand = str(raw_fac.get("url") or "").strip()
                if cand.startswith("https://"):
                    fac = cand
        key = (str(rail or ""), str(asset or ""), str(amount if amount is not None else ""), str(pay_to or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "source": source,
                "rail": _text(rail),
                "network": _text(acc.get("network")),
                "asset": _text(asset),
                "amount_atomic": str(amount) if amount is not None and amount != "" else None,
                "payTo": _text(pay_to),
                "facilitator": _text(fac),
            }
        )
    return out


def _fields_from_item(item: dict) -> dict:
    tags = item.get("tags")
    if isinstance(tags, list):
        tag_text = " ".join(str(t)[:80] for t in tags[:16] if str(t).strip())
    elif tags:
        tag_text = str(tags)[:200]
    else:
        tag_text = ""
    method = None
    try:
        method = probe.extract_method(item)
    except Exception:
        method = None
    return {
        "canonical_url": probe._resource_url(item),
        "service_name": _text(item.get("serviceName"))[:120] if _text(item.get("serviceName")) else None,
        "description": _text(item.get("description"))[:500] if _text(item.get("description")) else None,
        "capability": _text(item.get("capability")),
        "method": _text(method),
        "tags": tag_text or None,
        "input_schema_present": 1 if item.get("_input_schema_present") else 0,
        "output_schema_present": 1 if item.get("_output_schema_present") else 0,
        "source_resource_id": _text(item.get("id") or item.get("resourceId") or item.get("resource_id")),
    }


def row_hash(fields: dict, claims: list[dict]) -> str:
    """Normalized claim hash. Not a raw upstream payload."""
    accepts = sorted(
        (
            c.get("rail") or "",
            c.get("network") or "",
            c.get("asset") or "",
            c.get("amount_atomic") or "",
            c.get("payTo") or "",
            c.get("facilitator") or "",
        )
        for c in claims
    )
    blob = {
        "capability": fields.get("capability") or "",
        "description": fields.get("description") or "",
        "in": int(fields.get("input_schema_present") or 0),
        "method": fields.get("method") or "",
        "out": int(fields.get("output_schema_present") or 0),
        "service_name": fields.get("service_name") or "",
        "tags": fields.get("tags") or "",
        "accepts": accepts,
    }
    return hashlib.sha256(
        json.dumps(blob, separators=(",", ":"), sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _insert_event(cur, *, resource_id, url, event, source, detail, ts) -> None:
    payload = None
    if detail is not None:
        if isinstance(detail, (dict, list)):
            payload = json.dumps(detail, separators=(",", ":"), sort_keys=True)[:400]
        else:
            payload = str(detail)[:400]
    cur.execute(
        """
        INSERT INTO claim_events (resource_id, canonical_url, event, source, detail, ts)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (resource_id, url, event, source, payload, ts),
    )


def _cap_events(cur) -> None:
    cur.execute("SELECT COUNT(*) FROM claim_events")
    n = int(cur.fetchone()[0] or 0)
    if n <= EVENT_CAP:
        return
    drop = n - EVENT_CAP
    cur.execute(
        "DELETE FROM claim_events WHERE id IN (SELECT id FROM claim_events ORDER BY ts ASC, id ASC LIMIT ?)",
        (drop,),
    )


def _sync_fts(cur, resource_id: int, fields: dict) -> None:
    if not _fts_ok:
        return
    cur.execute("DELETE FROM resource_fts WHERE rowid = ?", (resource_id,))
    cur.execute(
        """
        INSERT INTO resource_fts (rowid, service_name, description, tags, capability, url)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            resource_id,
            fields.get("service_name") or "",
            fields.get("description") or "",
            fields.get("tags") or "",
            fields.get("capability") or "",
            fields.get("canonical_url") or "",
        ),
    )


def _load_claims(cur, resource_id: int, source: str | None = None) -> list[dict]:
    if source:
        cur.execute(
            """
            SELECT source, rail, network, asset, amount_atomic, payTo, facilitator
            FROM accept_claims WHERE resource_id = ? AND source = ?
            """,
            (resource_id, source),
        )
    else:
        cur.execute(
            """
            SELECT source, rail, network, asset, amount_atomic, payTo, facilitator
            FROM accept_claims WHERE resource_id = ?
            """,
            (resource_id,),
        )
    rows = []
    for r in cur.fetchall():
        rows.append(
            {
                "source": r["source"],
                "rail": r["rail"],
                "network": r["network"],
                "asset": r["asset"],
                "amount_atomic": r["amount_atomic"],
                "payTo": r["payTo"],
                "facilitator": r["facilitator"],
            }
        )
    return rows


def _replace_claims(cur, resource_id: int, source: str, claims: list[dict], url: str, ts: int) -> list[str]:
    old = _load_claims(cur, resource_id, source)
    events: list[str] = []
    old_rails = {c.get("rail") for c in old if c.get("rail")}
    new_rails = {c.get("rail") for c in claims if c.get("rail")}
    for rail in sorted(new_rails - old_rails):
        _insert_event(
            cur,
            resource_id=resource_id,
            url=url,
            event=EVENT_RAIL_ADDED,
            source=source,
            detail={"rail": rail},
            ts=ts,
        )
        events.append(EVENT_RAIL_ADDED)
    for rail in sorted(old_rails - new_rails):
        _insert_event(
            cur,
            resource_id=resource_id,
            url=url,
            event=EVENT_RAIL_REMOVED,
            source=source,
            detail={"rail": rail},
            ts=ts,
        )
        events.append(EVENT_RAIL_REMOVED)

    def _by_rail(rows):
        out: dict[str, dict] = {}
        for c in rows:
            rail = c.get("rail") or ""
            if rail and rail not in out:
                out[rail] = c
        return out

    old_by = _by_rail(old)
    new_by = _by_rail(claims)
    for rail, nxt in new_by.items():
        prev = old_by.get(rail)
        if not prev:
            continue
        if (prev.get("amount_atomic") or "") != (nxt.get("amount_atomic") or ""):
            _insert_event(
                cur,
                resource_id=resource_id,
                url=url,
                event=EVENT_PRICE,
                source=source,
                detail={"rail": rail, "old": prev.get("amount_atomic"), "new": nxt.get("amount_atomic")},
                ts=ts,
            )
            events.append(EVENT_PRICE)
        if (prev.get("payTo") or "") != (nxt.get("payTo") or ""):
            if not payment.payto_equal(prev.get("payTo"), nxt.get("payTo"), rail):
                _insert_event(
                    cur,
                    resource_id=resource_id,
                    url=url,
                    event=EVENT_PAYTO,
                    source=source,
                    detail={"rail": rail},
                    ts=ts,
                )
                events.append(EVENT_PAYTO)

    cur.execute("DELETE FROM accept_claims WHERE resource_id = ? AND source = ?", (resource_id, source))
    for c in claims:
        cur.execute(
            """
            INSERT INTO accept_claims (
                resource_id, source, rail, network, asset, amount_atomic, payTo, facilitator
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resource_id,
                source,
                c.get("rail"),
                c.get("network"),
                c.get("asset"),
                c.get("amount_atomic"),
                c.get("payTo"),
                c.get("facilitator"),
            ),
        )
    return events


def _upsert_source(cur, resource_id: int, source: str, source_resource_id, generation, ts: int) -> None:
    cur.execute(
        """
        INSERT INTO resource_sources (resource_id, source, source_resource_id, source_generation, source_last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(resource_id, source) DO UPDATE SET
            source_resource_id = COALESCE(excluded.source_resource_id, resource_sources.source_resource_id),
            source_generation = COALESCE(excluded.source_generation, resource_sources.source_generation),
            source_last_seen = excluded.source_last_seen
        """,
        (resource_id, source, source_resource_id, generation, ts),
    )


def _upsert_one(cur, item: dict, source: str, generation: int | None, ts: int) -> dict:
    fields = _fields_from_item(item)
    url = fields.get("canonical_url")
    if not url:
        return {"id": None, "events": [], "created": False}
    claims = _claims_from_item(item, source)
    digest = row_hash(fields, claims)
    cur.execute(
        """
        SELECT id, row_hash, status, input_schema_present, output_schema_present
        FROM resources WHERE canonical_url = ?
        """,
        (url,),
    )
    row = cur.fetchone()
    events: list[str] = []
    created = False
    if row is None:
        cur.execute(
            """
            INSERT INTO resources (
                canonical_url, service_name, description, capability, method, tags,
                input_schema_present, output_schema_present,
                first_seen, last_seen, last_fetched, row_hash, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                url,
                fields["service_name"],
                fields["description"],
                fields["capability"],
                fields["method"],
                fields["tags"],
                fields["input_schema_present"],
                fields["output_schema_present"],
                ts,
                ts,
                ts,
                digest,
                STATUS_ACTIVE,
            ),
        )
        resource_id = int(cur.lastrowid)
        created = True
        _insert_event(
            cur,
            resource_id=resource_id,
            url=url,
            event=EVENT_ADDED,
            source=source,
            detail=None,
            ts=ts,
        )
        events.append(EVENT_ADDED)
    else:
        resource_id = int(row["id"])
        prev_status = row["status"]
        reappeared = None
        status = STATUS_ACTIVE
        if prev_status == STATUS_RETIRED:
            reappeared = ts
        if int(row["input_schema_present"] or 0) != int(fields["input_schema_present"] or 0) or int(
            row["output_schema_present"] or 0
        ) != int(fields["output_schema_present"] or 0):
            _insert_event(
                cur,
                resource_id=resource_id,
                url=url,
                event=EVENT_SCHEMA,
                source=source,
                detail={
                    "old_in": row["input_schema_present"],
                    "new_in": fields["input_schema_present"],
                    "old_out": row["output_schema_present"],
                    "new_out": fields["output_schema_present"],
                },
                ts=ts,
            )
            events.append(EVENT_SCHEMA)
        cur.execute(
            """
            UPDATE resources SET
                service_name = ?,
                description = ?,
                capability = ?,
                method = ?,
                tags = ?,
                input_schema_present = ?,
                output_schema_present = ?,
                last_seen = ?,
                last_fetched = ?,
                row_hash = ?,
                status = ?,
                retired_at = CASE WHEN ? = 'active' THEN NULL ELSE retired_at END,
                reappeared_at = COALESCE(?, reappeared_at)
            WHERE id = ?
            """,
            (
                fields["service_name"],
                fields["description"],
                fields["capability"],
                fields["method"],
                fields["tags"],
                fields["input_schema_present"],
                fields["output_schema_present"],
                ts,
                ts,
                digest,
                status,
                status,
                reappeared,
                resource_id,
            ),
        )
    events.extend(_replace_claims(cur, resource_id, source, claims, url, ts))
    _upsert_source(cur, resource_id, source, fields.get("source_resource_id"), generation, ts)
    _sync_fts(cur, resource_id, fields)
    return {"id": resource_id, "events": events, "created": created, "url": url}


def upsert_item(item: dict | None, *, source: str, generation: int | None = None, ts: int | None = None) -> dict:
    """Upsert one slim catalog claim. Commits. Caller should discard the item."""
    if not isinstance(item, dict):
        return {"id": None, "events": [], "created": False}
    src = (source or "").strip() or SOURCE_CDP
    when = _as_int(ts, None) or _now()
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            out = _upsert_one(cur, item, src, generation, when)
            _cap_events(cur)
            conn.commit()
            _chmod_db_files(_conn_path or db_path())
        return out
    except Exception:
        return {"id": None, "events": [], "created": False}


def upsert_items(items, *, source: str, generation: int | None = None, ts: int | None = None) -> dict:
    """Upsert a need-scoped or one-page batch. Commit once. Do not keep the list."""
    rows = [i for i in (items or []) if isinstance(i, dict)]
    src = (source or "").strip() or SOURCE_CDP
    when = _as_int(ts, None) or _now()
    n = 0
    events: list[str] = []
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            for item in rows:
                out = _upsert_one(cur, item, src, generation, when)
                if out.get("id"):
                    n += 1
                    events.extend(out.get("events") or [])
            _cap_events(cur)
            conn.commit()
            _chmod_db_files(_conn_path or db_path())
        return {"upserted": n, "events": events}
    except Exception:
        return {"upserted": 0, "events": []}


def ingest_page(
    source: str,
    items,
    *,
    offset: int,
    last: bool = False,
    upstream_total=None,
    step: int | None = None,
    ts: int | None = None,
) -> dict:
    """Sweep one page: upsert, commit, advance cursor, discard. Never returns items."""
    src = (source or "").strip()
    if src not in SOURCES:
        return {"upserted": 0, "error": "unknown_source", "complete": False}
    when = _as_int(ts, None) or _now()
    try:
        off = int(offset)
    except (TypeError, ValueError):
        off = 0
    if off < 0:
        off = 0
    n_items = len([i for i in (items or []) if isinstance(i, dict)])
    adv = _as_int(step, None)
    if adv is None or adv < 1:
        adv = n_items
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            state = _source_state_unlocked(cur, src)
            gen = int(state.get("generation") or 0)
            if gen < 1:
                gen = 1
                cur.execute(
                    """
                    INSERT INTO source_state (source, generation, cursor, sweep_started_at)
                    VALUES (?, ?, 0, ?)
                    ON CONFLICT(source) DO UPDATE SET
                        generation = excluded.generation,
                        sweep_started_at = COALESCE(source_state.sweep_started_at, excluded.sweep_started_at)
                    """,
                    (src, gen, when),
                )
            upserted = 0
            events: list[str] = []
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                out = _upsert_one(cur, item, src, gen, when)
                if out.get("id"):
                    upserted += 1
                    events.extend(out.get("events") or [])
            new_cursor = off + max(int(adv), 0)
            total = _as_int(upstream_total, None)
            cur.execute(
                """
                UPDATE source_state SET
                    cursor = ?,
                    upstream_total = COALESCE(?, upstream_total)
                WHERE source = ?
                """,
                (new_cursor, total, src),
            )
            retired = 0
            complete = False
            if last or (total is not None and new_cursor >= total):
                retired = _retire_unseen_unlocked(cur, src, gen, when)
                cur.execute(
                    """
                    UPDATE source_state SET
                        last_complete_sweep_at = ?,
                        cursor = 0,
                        sweep_started_at = NULL
                    WHERE source = ?
                    """,
                    (when, src),
                )
                complete = True
            _cap_events(cur)
            conn.commit()
            _chmod_db_files(_conn_path or db_path())
        return {
            "upserted": upserted,
            "events": events,
            "cursor": 0 if complete else new_cursor,
            "generation": gen,
            "retired": retired,
            "complete": complete,
        }
    except Exception:
        return {"upserted": 0, "error": "ingest_failed", "complete": False}


def _source_state_unlocked(cur, source: str) -> dict:
    cur.execute(
        """
        SELECT source, generation, cursor, upstream_total, sweep_started_at, last_complete_sweep_at
        FROM source_state WHERE source = ?
        """,
        (source,),
    )
    row = cur.fetchone()
    if not row:
        return {
            "source": source,
            "generation": 0,
            "cursor": 0,
            "upstream_total": None,
            "sweep_started_at": None,
            "last_complete_sweep_at": None,
        }
    return {
        "source": row["source"],
        "generation": int(row["generation"] or 0),
        "cursor": int(row["cursor"] or 0),
        "upstream_total": row["upstream_total"],
        "sweep_started_at": row["sweep_started_at"],
        "last_complete_sweep_at": row["last_complete_sweep_at"],
    }


def source_state(source: str) -> dict:
    src = (source or "").strip()
    try:
        with _lock:
            conn = _connect()
            return _source_state_unlocked(conn.cursor(), src)
    except Exception:
        return {
            "source": src,
            "generation": 0,
            "cursor": 0,
            "upstream_total": None,
            "sweep_started_at": None,
            "last_complete_sweep_at": None,
        }


def begin_sweep(source: str, ts: int | None = None) -> int:
    """Start a generation sweep. Cursor reset. Returns generation."""
    src = (source or "").strip()
    if src not in SOURCES:
        return 0
    when = _as_int(ts, None) or _now()
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            state = _source_state_unlocked(cur, src)
            gen = int(state.get("generation") or 0) + 1
            cur.execute(
                """
                INSERT INTO source_state (source, generation, cursor, sweep_started_at)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(source) DO UPDATE SET
                    generation = excluded.generation,
                    cursor = 0,
                    sweep_started_at = excluded.sweep_started_at
                """,
                (src, gen, when),
            )
            conn.commit()
            _chmod_db_files(_conn_path or db_path())
        return gen
    except Exception:
        return 0


def _retire_unseen_unlocked(cur, source: str, generation: int, ts: int) -> int:
    """Mark previously active listings unseen this generation as retired. Do not delete."""
    cur.execute(
        """
        SELECT r.id, r.canonical_url, r.status
        FROM resources r
        JOIN resource_sources rs ON rs.resource_id = r.id
        WHERE rs.source = ?
          AND r.status = ?
          AND (rs.source_generation IS NULL OR rs.source_generation != ?)
        """,
        (source, STATUS_ACTIVE, generation),
    )
    candidates = list(cur.fetchall())
    retired = 0
    for row in candidates:
        rid = int(row["id"])
        cur.execute(
            """
            SELECT 1 FROM resource_sources
            WHERE resource_id = ? AND source != ?
              AND source_generation IS NOT NULL
              AND source_last_seen IS NOT NULL
              AND source_last_seen >= ?
            LIMIT 1
            """,
            (rid, source, ts - cold_sweep_s()),
        )
        if cur.fetchone():
            continue
        cur.execute(
            """
            UPDATE resources SET status = ?, retired_at = ? WHERE id = ? AND status = ?
            """,
            (STATUS_RETIRED, ts, rid, STATUS_ACTIVE),
        )
        if cur.rowcount:
            _insert_event(
                cur,
                resource_id=rid,
                url=row["canonical_url"],
                event=EVENT_RETIRED,
                source=source,
                detail={"generation": generation},
                ts=ts,
            )
            retired += 1
    return retired


def complete_sweep(source: str, ts: int | None = None) -> dict:
    """Finish a generation: unseen this source → retired. Rows stay on disk."""
    src = (source or "").strip()
    if src not in SOURCES:
        return {"retired": 0, "generation": 0}
    when = _as_int(ts, None) or _now()
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            state = _source_state_unlocked(cur, src)
            gen = int(state.get("generation") or 0)
            if gen < 1:
                return {"retired": 0, "generation": 0}
            retired = _retire_unseen_unlocked(cur, src, gen, when)
            cur.execute(
                """
                UPDATE source_state SET
                    last_complete_sweep_at = ?,
                    cursor = 0,
                    sweep_started_at = NULL
                WHERE source = ?
                """,
                (when, src),
            )
            _cap_events(cur)
            conn.commit()
            _chmod_db_files(_conn_path or db_path())
        return {"retired": retired, "generation": gen}
    except Exception:
        return {"retired": 0, "generation": 0}


def _fts_match(need: str) -> str:
    toks: list[str] = []
    for raw in _FTS_TOKEN.findall(need or ""):
        low = raw.lower()
        if low in probe.STOP:
            continue
        safe = raw.replace('"', "")
        if not safe:
            continue
        toks.append(safe)
        if len(toks) >= 8:
            break
    if not toks:
        return ""
    return " OR ".join('"%s"' % t for t in toks)


def _like_ids(cur, need: str, limit: int) -> list[int]:
    toks = [t.lower() for t in _FTS_TOKEN.findall(need or "") if t.lower() not in probe.STOP][:4]
    if not toks:
        return []
    clauses = []
    args: list = []
    for tok in toks:
        needle = "%%%s%%" % tok.replace("%", "").replace("_", "")
        clauses.append(
            "(LOWER(COALESCE(service_name,'')) LIKE ? OR LOWER(COALESCE(description,'')) LIKE ? "
            "OR LOWER(COALESCE(tags,'')) LIKE ? OR LOWER(COALESCE(capability,'')) LIKE ? "
            "OR LOWER(canonical_url) LIKE ?)"
        )
        args.extend([needle] * 5)
    sql = (
        "SELECT id FROM resources WHERE status = ? AND (%s) ORDER BY last_seen DESC LIMIT ?"
        % " OR ".join(clauses)
    )
    args = [STATUS_ACTIVE, *args, limit]
    cur.execute(sql, args)
    return [int(r[0]) for r in cur.fetchall()]


def _reconstruct(cur, resource_id: int) -> dict | None:
    cur.execute("SELECT * FROM resources WHERE id = ?", (resource_id,))
    row = cur.fetchone()
    if not row:
        return None
    claims = _load_claims(cur, resource_id)
    accepts = []
    rails: list[str] = []
    for c in claims:
        acc: dict = {}
        if c.get("network"):
            acc["network"] = c["network"]
        if c.get("asset"):
            acc["asset"] = c["asset"]
        if c.get("amount_atomic") is not None:
            acc["amount"] = c["amount_atomic"]
        if c.get("payTo"):
            acc["payTo"] = c["payTo"]
        if c.get("facilitator"):
            acc["extra"] = {"facilitator": c["facilitator"]}
        if acc:
            accepts.append(acc)
        rail = c.get("rail")
        if rail and rail not in rails:
            rails.append(rail)
    if not rails:
        cur.execute("SELECT source FROM resource_sources WHERE resource_id = ?", (resource_id,))
        for r in cur.fetchall():
            rail = rail_for_source(r["source"])
            if rail not in rails:
                rails.append(rail)
    primary = rails[0] if rails else "base"
    tags = []
    if row["tags"]:
        tags = [t for t in str(row["tags"]).split() if t]
    item = {
        "url": row["canonical_url"],
        "serviceName": row["service_name"],
        "description": row["description"],
        "capability": row["capability"] or "unknown",
        "capability_source": "shadow",
        "tags": tags,
        "_input_schema_present": bool(row["input_schema_present"]),
        "_output_schema_present": bool(row["output_schema_present"]),
        "_rail": primary,
        "rails": rails or [primary],
    }
    if accepts:
        item["accepts"] = accepts
    if len(rails) > 1:
        item["also_on"] = [r for r in rails if r != primary]
    item["_clocks"] = {
        "discovery": row["last_seen"],
        "claim": row["last_fetched"],
        "verification": row["last_verified"],
    }
    return item


def fts_search(need: str, *, rails=None, limit: int = FTS_LIMIT, include_retired: bool = False) -> list[dict]:
    """Need-scoped FTS. Never loads the world. Retired excluded unless asked."""
    q = " ".join((need or "").split())
    if not q:
        return []
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = FTS_LIMIT
    cap = min(max(cap, 1), FTS_LIMIT)
    want = None
    if rails:
        want = {str(r).strip() for r in rails if str(r).strip()}
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            ids: list[int] = []
            match = _fts_match(q)
            if _fts_ok and match:
                cur.execute(
                    """
                    SELECT f.rowid
                    FROM resource_fts f
                    JOIN resources r ON r.id = f.rowid
                    WHERE resource_fts MATCH ?
                      AND (? OR r.status = ?)
                    ORDER BY r.last_seen DESC
                    LIMIT ?
                    """,
                    (match, 1 if include_retired else 0, STATUS_ACTIVE, cap * 3),
                )
                ids = [int(r[0]) for r in cur.fetchall()]
            if not ids:
                ids = _like_ids(cur, q, cap * 3)
            items: list[dict] = []
            seen: set[str] = set()
            for rid in ids:
                item = _reconstruct(cur, rid)
                if not item:
                    continue
                url = item.get("url")
                if not url or url in seen:
                    continue
                if want:
                    item_rails = set(item.get("rails") or [item.get("_rail")])
                    if not (item_rails & want):
                        continue
                seen.add(url)
                items.append(item)
                if len(items) >= cap:
                    break
            return items
    except Exception:
        return []


def get_resource(url: str) -> dict | None:
    dest = _text(url)
    if not dest:
        return None
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            cur.execute("SELECT id FROM resources WHERE canonical_url = ?", (dest,))
            row = cur.fetchone()
            if not row:
                return None
            return _reconstruct(cur, int(row["id"]))
    except Exception:
        return None


def accept_claims(url: str) -> list[dict]:
    """Claimed payment terms only. Never feed these to observed selection."""
    dest = _text(url)
    if not dest:
        return []
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            cur.execute("SELECT id FROM resources WHERE canonical_url = ?", (dest,))
            row = cur.fetchone()
            if not row:
                return []
            return _load_claims(cur, int(row["id"]))
    except Exception:
        return []


def clocks(url: str) -> dict:
    """Three clocks. Never one collapsed freshness field."""
    out = {"discovery": None, "claim": None, "verification": None}
    dest = _text(url)
    if not dest:
        return out
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT last_seen, last_fetched, last_verified FROM resources WHERE canonical_url = ?",
                (dest,),
            )
            row = cur.fetchone()
        if not row:
            return out
        return {
            "discovery": row["last_seen"],
            "claim": row["last_fetched"],
            "verification": row["last_verified"],
        }
    except Exception:
        return out


def clocks_iso(url: str) -> dict:
    raw = clocks(url)
    return {k: _iso_ts(v) for k, v in raw.items()}


def listing_facts(url: str) -> dict:
    """Catalog tenure and independent source count. Missing != 0.

    first_seen / days_listed / source_count are None when the URL is not
    on catalog.sqlite. Never invent a zero-source listing.
    """
    out = {
        "first_seen": None,
        "days_listed": None,
        "source_count": None,
        "claimed_rails": None,
    }
    dest = _text(url)
    if not dest:
        return out
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT id, first_seen FROM resources WHERE canonical_url = ?",
                (dest,),
            )
            row = cur.fetchone()
            if not row:
                return out
            rid = int(row["id"])
            first = _as_int(row["first_seen"], None)
            out["first_seen"] = first
            if first is not None:
                out["days_listed"] = max(0, int((_now() - first) / 86400))
            cur.execute(
                "SELECT COUNT(*) AS n FROM resource_sources WHERE resource_id = ?",
                (rid,),
            )
            nsrc = cur.fetchone()
            if nsrc is not None:
                out["source_count"] = int(nsrc["n"] or 0)
            cur.execute(
                "SELECT DISTINCT rail FROM accept_claims WHERE resource_id = ? AND rail IS NOT NULL",
                (rid,),
            )
            rails = [r["rail"] for r in cur.fetchall() if r["rail"]]
            if rails:
                out["claimed_rails"] = rails
        return out
    except Exception:
        return {
            "first_seen": None,
            "days_listed": None,
            "source_count": None,
            "claimed_rails": None,
        }


def resource_status(url: str) -> str | None:
    dest = _text(url)
    if not dest:
        return None
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            cur.execute("SELECT status FROM resources WHERE canonical_url = ?", (dest,))
            row = cur.fetchone()
        return row["status"] if row else None
    except Exception:
        return None


def resource_count(status: str | None = None) -> int:
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            if status:
                cur.execute("SELECT COUNT(*) FROM resources WHERE status = ?", (status,))
            else:
                cur.execute("SELECT COUNT(*) FROM resources")
            return int(cur.fetchone()[0] or 0)
    except Exception:
        return 0


def touch_searched(urls, ts: int | None = None) -> None:
    _touch_urls(urls, "last_searched", ts)


def touch_routed(urls, ts: int | None = None) -> None:
    _touch_urls(urls, "last_routed", ts)


def mark_verified(url: str, ts: int | None = None, ok: bool | None = None) -> None:
    """Verification clock only. Observation rows stay in history.py.

    ok=True/False records last_probe_ok for the refresh queue. Missing ok
    leaves last_probe_ok unchanged (unknown is not a failed probe).
    """
    dest = _text(url)
    if not dest:
        return
    when = _as_int(ts, None) or _now()
    try:
        with _lock:
            conn = _connect()
            if ok is None:
                conn.execute(
                    "UPDATE resources SET last_verified = ? WHERE canonical_url = ?",
                    (when, dest),
                )
            else:
                conn.execute(
                    "UPDATE resources SET last_verified = ?, last_probe_ok = ? WHERE canonical_url = ?",
                    (when, 1 if ok else 0, dest),
                )
            conn.commit()
    except Exception:
        return


def _touch_urls(urls, column: str, ts: int | None) -> None:
    if column not in {"last_searched", "last_routed"}:
        return
    dests: list[str] = []
    seen: set[str] = set()
    for raw in urls or []:
        dest = _text(raw)
        if not dest or dest in seen:
            continue
        seen.add(dest)
        dests.append(dest)
    if not dests:
        return
    when = _as_int(ts, None) or _now()
    try:
        with _lock:
            conn = _connect()
            conn.executemany(
                f"UPDATE resources SET {column} = ? WHERE canonical_url = ?",
                [(when, u) for u in dests],
            )
            conn.commit()
    except Exception:
        return


def due_hot(limit: int = 5, ts: int | None = None) -> list[str]:
    when = _as_int(ts, None) or _now()
    try:
        cap = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        cap = 5
    eligible = when - HOT_ELIGIBLE_S
    stale = when - hot_refresh_s()
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT canonical_url FROM resources
                WHERE status = ?
                  AND (COALESCE(last_searched, 0) >= ? OR COALESCE(last_routed, 0) >= ?)
                  AND COALESCE(last_fetched, 0) < ?
                ORDER BY MAX(COALESCE(last_searched, 0), COALESCE(last_routed, 0)) DESC
                LIMIT ?
                """,
                (STATUS_ACTIVE, eligible, eligible, stale, cap),
            )
            return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


def due_warm(limit: int = 5, ts: int | None = None) -> list[str]:
    when = _as_int(ts, None) or _now()
    try:
        cap = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        cap = 5
    hot_eligible = when - HOT_ELIGIBLE_S
    warm_eligible = when - WARM_ELIGIBLE_S
    stale = when - warm_refresh_s()
    hot_stale = when - hot_refresh_s()
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT canonical_url FROM resources
                WHERE status = ?
                  AND COALESCE(last_fetched, 0) < ?
                  AND (
                    COALESCE(last_searched, 0) >= ?
                    OR COALESCE(last_routed, 0) >= ?
                    OR COALESCE(last_seen, 0) >= ?
                  )
                  AND NOT (
                    (COALESCE(last_searched, 0) >= ? OR COALESCE(last_routed, 0) >= ?)
                    AND COALESCE(last_fetched, 0) < ?
                  )
                ORDER BY last_fetched ASC
                LIMIT ?
                """,
                (
                    STATUS_ACTIVE,
                    stale,
                    warm_eligible,
                    warm_eligible,
                    warm_eligible,
                    hot_eligible,
                    hot_eligible,
                    hot_stale,
                    cap,
                ),
            )
            return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


def _queue_cap(limit) -> int:
    try:
        return max(1, min(int(limit), QUEUE_SCAN))
    except (TypeError, ValueError):
        return 5


def _due_urls_for_reason(cur, reason: str, when: int, stale: int, recent: int, change_since: int) -> list[str]:
    """Need-scoped candidates for one reason. LIMIT QUEUE_SCAN. Never the world."""
    scan = QUEUE_SCAN
    if reason == "recent_search":
        cur.execute(
            """
            SELECT canonical_url FROM resources
            WHERE status = ?
              AND COALESCE(last_searched, 0) >= ?
              AND COALESCE(last_fetched, 0) < ?
            ORDER BY last_searched DESC, canonical_url ASC
            LIMIT ?
            """,
            (STATUS_ACTIVE, recent, stale, scan),
        )
    elif reason == "recent_route":
        cur.execute(
            """
            SELECT canonical_url FROM resources
            WHERE status = ?
              AND COALESCE(last_routed, 0) >= ?
              AND COALESCE(last_fetched, 0) < ?
            ORDER BY last_routed DESC, canonical_url ASC
            LIMIT ?
            """,
            (STATUS_ACTIVE, recent, stale, scan),
        )
    elif reason == "source_disagreement":
        cur.execute(
            """
            SELECT r.canonical_url
            FROM resources r
            JOIN accept_claims a ON a.resource_id = r.id
            WHERE r.status = ?
              AND COALESCE(r.last_fetched, 0) < ?
            GROUP BY r.id, r.canonical_url, a.rail
            HAVING COUNT(DISTINCT a.source) > 1
               AND (
                    COUNT(DISTINCT IFNULL(a.amount_atomic, '')) > 1
                    OR COUNT(DISTINCT IFNULL(a.payTo, '')) > 1
               )
            ORDER BY r.last_fetched ASC, r.canonical_url ASC
            LIMIT ?
            """,
            (STATUS_ACTIVE, stale, scan),
        )
    elif reason == "price_change":
        cur.execute(
            """
            SELECT DISTINCT e.canonical_url
            FROM claim_events e
            JOIN resources r ON r.canonical_url = e.canonical_url
            WHERE e.event = ?
              AND e.ts >= ?
              AND r.status = ?
              AND COALESCE(r.last_fetched, 0) < ?
            ORDER BY e.ts DESC, e.canonical_url ASC
            LIMIT ?
            """,
            (EVENT_PRICE, change_since, STATUS_ACTIVE, stale, scan),
        )
    elif reason == "payto_change":
        cur.execute(
            """
            SELECT DISTINCT e.canonical_url
            FROM claim_events e
            JOIN resources r ON r.canonical_url = e.canonical_url
            WHERE e.event = ?
              AND e.ts >= ?
              AND r.status = ?
              AND COALESCE(r.last_fetched, 0) < ?
            ORDER BY e.ts DESC, e.canonical_url ASC
            LIMIT ?
            """,
            (EVENT_PAYTO, change_since, STATUS_ACTIVE, stale, scan),
        )
    elif reason == "schema_change":
        cur.execute(
            """
            SELECT DISTINCT e.canonical_url
            FROM claim_events e
            JOIN resources r ON r.canonical_url = e.canonical_url
            WHERE e.event = ?
              AND e.ts >= ?
              AND r.status = ?
              AND COALESCE(r.last_fetched, 0) < ?
            ORDER BY e.ts DESC, e.canonical_url ASC
            LIMIT ?
            """,
            (EVENT_SCHEMA, change_since, STATUS_ACTIVE, stale, scan),
        )
    elif reason == "failed_probe":
        cur.execute(
            """
            SELECT canonical_url FROM resources
            WHERE status = ?
              AND last_probe_ok = 0
              AND COALESCE(last_fetched, 0) < ?
            ORDER BY COALESCE(last_verified, 0) DESC, canonical_url ASC
            LIMIT ?
            """,
            (STATUS_ACTIVE, stale, scan),
        )
    elif reason == "stale_observation":
        cur.execute(
            """
            SELECT canonical_url FROM resources
            WHERE status = ?
              AND COALESCE(last_fetched, 0) < ?
              AND (last_verified IS NULL OR last_verified < ?)
            ORDER BY COALESCE(last_verified, 0) ASC, canonical_url ASC
            LIMIT ?
            """,
            (STATUS_ACTIVE, stale, when - WARM_ELIGIBLE_S, scan),
        )
    elif reason == "high_demand_capability":
        cur.execute(
            """
            SELECT capability FROM resources
            WHERE status = ?
              AND capability IS NOT NULL
              AND TRIM(capability) != ''
              AND capability != 'unknown'
              AND COALESCE(last_searched, 0) >= ?
            GROUP BY capability
            HAVING COUNT(*) >= ?
            ORDER BY COUNT(*) DESC, capability ASC
            LIMIT 3
            """,
            (STATUS_ACTIVE, recent, HIGH_DEMAND_MIN),
        )
        caps = [r[0] for r in cur.fetchall() if r[0]]
        if not caps:
            return []
        placeholders = ",".join("?" * len(caps))
        cur.execute(
            f"""
            SELECT canonical_url FROM resources
            WHERE status = ?
              AND capability IN ({placeholders})
              AND COALESCE(last_fetched, 0) < ?
            ORDER BY COALESCE(last_searched, 0) DESC, canonical_url ASC
            LIMIT ?
            """,
            (STATUS_ACTIVE, *caps, stale, scan),
        )
    else:
        return []
    return [r[0] for r in cur.fetchall() if r[0]]


def due_valued(limit: int = 5, ts: int | None = None) -> list[dict]:
    """Deterministic information-value queue. First matching reason wins.

    Only stale claims (last_fetched older than HOT refresh). Need-scoped
    scans (QUEUE_SCAN per reason). Never a 44k RAM rebuild. No extra
    network. Caller still uses the existing trickle probe/discovery budget.
    """
    when = _as_int(ts, None) or _now()
    cap = _queue_cap(limit)
    stale = when - hot_refresh_s()
    recent = when - HOT_ELIGIBLE_S
    change_since = when - WARM_ELIGIBLE_S
    out: list[dict] = []
    seen: set[str] = set()
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            for reason in REFRESH_REASONS:
                for url in _due_urls_for_reason(cur, reason, when, stale, recent, change_since):
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    out.append({"url": url, "reason": reason})
                    if len(out) >= cap:
                        return out
            return out
    except Exception:
        return []


def due_valued_urls(limit: int = 5, ts: int | None = None) -> list[str]:
    return [row["url"] for row in due_valued(limit, ts) if row.get("url")]


def next_cold_source(ts: int | None = None) -> str | None:
    """Source that should take the next single COLD page. None if all sweeps fresh."""
    when = _as_int(ts, None) or _now()
    cutoff = when - cold_sweep_s()
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            best = None
            best_key = None
            for src in SOURCES:
                state = _source_state_unlocked(cur, src)
                started = state.get("sweep_started_at")
                cursor = int(state.get("cursor") or 0)
                done = state.get("last_complete_sweep_at")
                if started and cursor > 0:
                    key = (0, int(started))
                elif done is None or int(done) <= cutoff:
                    key = (1, int(done or 0))
                else:
                    continue
                if best_key is None or key < best_key:
                    best_key = key
                    best = src
            return best
    except Exception:
        return None


def claim_events(url: str | None = None, limit: int = 50) -> list[dict]:
    try:
        cap = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        cap = 50
    dest = _text(url)
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            if dest:
                cur.execute(
                    """
                    SELECT resource_id, canonical_url, event, source, detail, ts
                    FROM claim_events WHERE canonical_url = ?
                    ORDER BY ts DESC, id DESC LIMIT ?
                    """,
                    (dest, cap),
                )
            else:
                cur.execute(
                    """
                    SELECT resource_id, canonical_url, event, source, detail, ts
                    FROM claim_events ORDER BY ts DESC, id DESC LIMIT ?
                    """,
                    (cap,),
                )
            out = []
            for r in cur.fetchall():
                detail = r["detail"]
                parsed = None
                if detail:
                    try:
                        parsed = json.loads(detail)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        parsed = detail
                out.append(
                    {
                        "resource_id": r["resource_id"],
                        "url": r["canonical_url"],
                        "event": r["event"],
                        "source": r["source"],
                        "detail": parsed,
                        "ts": r["ts"],
                    }
                )
            return out
    except Exception:
        return []


def db_size_bytes() -> int:
    path = db_path()
    total = 0
    for p in (path, path + "-wal", path + "-shm"):
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def stats() -> dict:
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM resources")
            n = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT COUNT(*) FROM resources WHERE status = ?", (STATUS_ACTIVE,))
            active = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT COUNT(*) FROM resources WHERE status = ?", (STATUS_RETIRED,))
            retired = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT COUNT(*) FROM accept_claims")
            claims = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT COUNT(*) FROM claim_events")
            events = int(cur.fetchone()[0] or 0)
        return {
            "path": db_path(),
            "bytes": db_size_bytes(),
            "resources": n,
            "active": active,
            "retired": retired,
            "accept_claims": claims,
            "claim_events": events,
            "fts": bool(_fts_ok),
        }
    except Exception:
        return {
            "path": db_path(),
            "bytes": db_size_bytes(),
            "resources": 0,
            "active": 0,
            "retired": 0,
            "accept_claims": 0,
            "claim_events": 0,
            "fts": False,
        }
