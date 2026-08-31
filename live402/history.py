"""Probe observation history. Stdlib sqlite3 only. Fail closed. Never pay."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

from live402 import payment

DEFAULT_DB = "/tmp/live402-history.sqlite"
VOLUME_DB = "/data/live402-history.sqlite"
PER_URL_CAP = 500
GLOBAL_CAP = 50_000
OBS_PER_URL_CAP = 4000
DAY = 86400
WEEK = 7 * DAY
MIN_HEALTHY_N = 10
ATTEST_ALGO = "sha256"

SOURCE_OBSERVED = "402signal_observed"
SOURCE_CLAIMED = "catalog_claimed"
SOURCE_LEGACY = "legacy_mixed"

OBSERVED_FIELDS = (
    "live",
    "payable",
    "invocable",
    "latency_ms",
    "payTo",
    "amount",
    "http_status",
    "schema_present",
)
CLAIMED_FIELDS = ("payTo", "amount", "schema_present", "facilitator", "source")
BOOLISH_FIELDS = frozenset({"live", "payable", "invocable", "schema_present"})
INTISH_FIELDS = frozenset({"http_status", "latency_ms", "schema_present", "payable", "invocable", "live"})

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    ts INTEGER NOT NULL,
    live INTEGER NOT NULL DEFAULT 0,
    payable INTEGER NOT NULL DEFAULT 0,
    invocable INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    payTo TEXT,
    amount TEXT,
    miss_reason TEXT,
    rail TEXT,
    schema_present INTEGER
);
CREATE INDEX IF NOT EXISTS probes_url_ts ON probes(url, ts);
CREATE INDEX IF NOT EXISTS probes_ts ON probes(ts);
CREATE TABLE IF NOT EXISTS url_state (
    url TEXT PRIMARY KEY,
    last_payTo TEXT,
    last_amount TEXT,
    schema_present INTEGER,
    payTo_changed_at INTEGER,
    price_changed_at INTEGER,
    schema_changed_at INTEGER,
    last_checked INTEGER,
    last_success_402 INTEGER
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    probe_id INTEGER,
    batch_id TEXT,
    source_type TEXT NOT NULL,
    source TEXT,
    rail TEXT,
    url TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT,
    status TEXT,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_url_field_ts ON observations(url, field, ts);
CREATE INDEX IF NOT EXISTS observations_probe_id ON observations(probe_id);
CREATE INDEX IF NOT EXISTS observations_source_type_ts ON observations(source_type, ts);
CREATE INDEX IF NOT EXISTS observations_batch_id ON observations(batch_id);
CREATE TABLE IF NOT EXISTS sealed_batches (
    batch_id TEXT PRIMARY KEY,
    sealed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS scoring_models (
    model_id TEXT NOT NULL,
    model_hash TEXT NOT NULL,
    effective_ts INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (model_id, model_hash)
);
"""


def db_path() -> str:
    raw = (os.environ.get("LIVE402_HISTORY_DB") or "").strip()
    if raw:
        return raw
    try:
        if os.path.isdir("/data") and os.access("/data", os.W_OK):
            return VOLUME_DB
    except Exception:
        pass
    return DEFAULT_DB


def _empty_summary() -> dict:
    return {
        "last_checked": None,
        "last_success_402": None,
        "n_24h": 0,
        "ok_24h": 0,
        "n_7d": 0,
        "ok_7d": 0,
        "success_24h": None,
        "success_7d": None,
        "p50_latency_ms": None,
        "p95_latency_ms": None,
        "last_payTo": None,
        "payTo_changed_at": None,
        "price_changed_at": None,
        "schema_changed_at": None,
    }


def _connect() -> sqlite3.Connection:
    global _conn, _conn_path
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
    conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    _conn = conn
    _conn_path = path
    _chmod_db_files(path)
    return conn


def _chmod_db_files(path: str) -> None:
    for p in (path, path + "-wal", path + "-shm"):
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


def reset() -> None:
    """Delete the history DB (tests)."""
    global _conn, _conn_path
    with _lock:
        path = _conn_path or db_path()
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
            _conn_path = None
        for p in (path, path + "-wal", path + "-shm"):
            try:
                os.remove(p)
            except OSError:
                pass


def _as_int(val, default=None):
    if val is None or val is False:
        return default
    if isinstance(val, bool):
        return int(val)
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _text(val) -> str | None:
    if val is None:
        return None
    text = str(val).strip()
    return text or None


def _payto(snap: dict) -> str | None:
    return _text(snap.get("payTo"))


def _envelope(snap: dict) -> dict:
    env = snap.get("envelope")
    return env if isinstance(env, dict) else {}


def _amount_from_accepts(accepts) -> str | None:
    if not isinstance(accepts, list):
        return None
    for acc in accepts:
        if not isinstance(acc, dict):
            continue
        raw = acc.get("amount")
        if raw is None:
            raw = acc.get("maxAmountRequired")
        text = _text(raw)
        if text:
            return text
    return None


def _observed_amount(snap: dict) -> str | None:
    """Runtime quote only: snap.amount or 402 envelope accepts. Never catalog target.amountAtomic."""
    if snap.get("amount") is not None and snap.get("amount") != "":
        return _text(snap.get("amount"))
    return _amount_from_accepts(_envelope(snap).get("accepts"))


def _observed_payable(snap: dict) -> bool:
    """payable iff live and at least one complete CURRENT observed option. Fail closed."""
    if not isinstance(snap, dict) or not snap.get("live"):
        return False
    env = _envelope(snap)
    for opt in payment.payment_options_from_result(snap):
        if payment.is_complete_payment_option(opt, env):
            return True
    return False


def _price_option(amount, snap: dict, rail=None) -> dict | None:
    opts = payment.payment_options_from_result(snap if isinstance(snap, dict) else {})
    if opts:
        return opts[0]
    return payment.payment_option_from_accept(
        {
            "amount": amount,
            "asset": (snap or {}).get("asset"),
            "network": (snap or {}).get("network") or rail,
        },
        rail,
    )


def _price_flipped(prev_amt, amount, snap: dict, rail=None) -> bool:
    """Asset-aware price change. Do not treat raw atomic strings as dollars."""
    curr = _price_option(amount, snap, rail)
    prev = payment.payment_option_from_accept(
        {
            "amount": prev_amt,
            "asset": (curr or {}).get("asset") or (snap or {}).get("asset"),
            "network": (curr or {}).get("network") or (snap or {}).get("network") or rail,
        },
        rail,
    )
    if curr and prev:
        if payment.prices_equivalent(prev, curr):
            return False
        # Same known asset, different atomic / USD → flipped.
        if payment.asset_identity(prev) and payment.asset_identity(prev) == payment.asset_identity(curr):
            return True
        if prev.get("normalized_usd") is not None and curr.get("normalized_usd") is not None:
            return float(prev["normalized_usd"]) != float(curr["normalized_usd"])
        # Different or unknown assets: only flip when atomics differ AND assets match
        # enough to compare. Incomparable assets are not a silent same-price.
        if not payment.asset_identity(prev) or not payment.asset_identity(curr):
            return str(prev_amt) != str(amount)
        return True
    return str(prev_amt) != str(amount)

def _bazaar_schema_present(blob: dict | None) -> bool:
    if not isinstance(blob, dict):
        return False
    ext = blob.get("extensions") if isinstance(blob.get("extensions"), dict) else {}
    bazaar = ext.get("bazaar") if isinstance(ext, dict) else None
    if not isinstance(bazaar, dict):
        return False
    info = bazaar.get("info") if isinstance(bazaar.get("info"), dict) else {}
    inp = info.get("input") if isinstance(info.get("input"), dict) else {}
    schema = inp.get("inputSchema") if isinstance(inp, dict) else None
    if isinstance(schema, dict) and (schema.get("properties") or schema.get("required") or schema.get("type")):
        return True
    inner = bazaar.get("schema") if isinstance(bazaar.get("schema"), dict) else {}
    props = (inner.get("properties") or {}).get("input") if isinstance(inner, dict) else None
    if isinstance(props, dict) and (props.get("properties") or props.get("required") or props.get("type")):
        return True
    return False


def _envelope_schema_present(env: dict) -> bool:
    schema = env.get("inputSchema")
    if isinstance(schema, dict) and (schema.get("properties") or schema.get("required") or schema.get("type")):
        return True
    return _bazaar_schema_present(env)


def _observed_schema_present(snap: dict) -> int | None:
    """Envelope-only. Never catalog bazaar, target.inputSchema, or a thin accepts envelope. None = unknown."""
    if snap.get("schema_present") is not None:
        return 1 if snap.get("schema_present") else 0
    source = _text(snap.get("schema_source"))
    env = _envelope(snap)
    if source == "envelope" and env and _envelope_schema_present(env):
        return 1
    if env and _envelope_schema_present(env):
        return 1
    return None


def _amount(snap: dict) -> str | None:
    """Observed amount only. Catalog target.amountAtomic is not an observation."""
    return _observed_amount(snap)


def _schema_present(snap: dict) -> int | None:
    """Observed schema only. Catalog inputSchema is not an observation."""
    return _observed_schema_present(snap)


def _percentile(values: list[int], p: float) -> int | None:
    if not values:
        return None
    xs = sorted(int(v) for v in values)
    if len(xs) == 1:
        return xs[0]
    k = (float(p) / 100.0) * (len(xs) - 1)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return xs[lo]
    return int(round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo)))


def _claimed_blob(snap: dict) -> dict | None:
    for key in ("claimed", "_claimed"):
        blob = snap.get(key)
        if isinstance(blob, dict):
            return blob
    return None


def _obs_text(field: str, raw) -> str | None:
    if raw is None or raw == "":
        return None
    if field in BOOLISH_FIELDS:
        if isinstance(raw, str) and raw.strip().lower() in ("unknown", "none"):
            return None
        return "1" if raw not in (0, "0", False, "false", "False") else "0"
    if field in ("http_status", "latency_ms"):
        n = _as_int(raw, None)
        return str(n) if n is not None else None
    return _text(raw)


def _insert_observation(
    cur,
    *,
    probe_id,
    batch_id,
    source_type: str,
    source,
    rail,
    url: str,
    field: str,
    value,
    status: str,
    ts: int,
) -> None:
    if value is None:
        return
    text = _obs_text(field, value)
    if text is None:
        return
    cur.execute(
        """
        INSERT INTO observations (probe_id, batch_id, source_type, source, rail, url, field, value, status, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (probe_id, batch_id, source_type, source, rail, url, field, text, status, ts),
    )


def _write_observed(cur, *, probe_id, batch_id, source, rail, url, ts, fields: dict) -> None:
    for field in OBSERVED_FIELDS:
        if field not in fields:
            continue
        val = fields[field]
        if val is None:
            continue
        _insert_observation(
            cur,
            probe_id=probe_id,
            batch_id=batch_id,
            source_type=SOURCE_OBSERVED,
            source=source,
            rail=rail,
            url=url,
            field=field,
            value=val,
            status="observed",
            ts=ts,
        )


def _write_claimed(cur, *, probe_id, batch_id, source, rail, url, ts, fields: dict) -> None:
    blob = fields if isinstance(fields, dict) else {}
    src = _text(blob.get("source")) or _text(source)
    r = _text(blob.get("rail")) or _text(rail)
    for field in CLAIMED_FIELDS:
        if field not in blob:
            continue
        raw = blob.get(field)
        if raw is None or raw == "":
            continue
        _insert_observation(
            cur,
            probe_id=probe_id,
            batch_id=batch_id,
            source_type=SOURCE_CLAIMED,
            source=src,
            rail=r,
            url=url,
            field=field,
            value=raw,
            status="claimed",
            ts=ts,
        )


def _delete_probes_and_obs(cur, ids: list) -> None:
    if not ids:
        return
    extra = [(i,) for i in ids]
    cur.executemany("DELETE FROM probes WHERE id = ?", extra)
    cur.executemany("DELETE FROM observations WHERE probe_id = ?", extra)


def _cap_observations(cur, url: str) -> None:
    cur.execute(
        "SELECT id FROM observations WHERE url = ? ORDER BY ts DESC, id DESC",
        (url,),
    )
    ids = [r[0] for r in cur.fetchall()]
    if len(ids) > OBS_PER_URL_CAP:
        extra = [(i,) for i in ids[OBS_PER_URL_CAP:]]
        cur.executemany("DELETE FROM observations WHERE id = ?", extra)


def _is_sealed_unlocked(batch_id: str | None) -> bool:
    bid = _text(batch_id)
    if not bid:
        return False
    conn = _connect()
    row = conn.execute(
        "SELECT 1 FROM sealed_batches WHERE batch_id = ? LIMIT 1",
        (bid,),
    ).fetchone()
    return bool(row)


def _seal_unlocked(batch_id: str) -> None:
    conn = _connect()
    conn.execute(
        "INSERT OR IGNORE INTO sealed_batches (batch_id, sealed_at) VALUES (?, ?)",
        (batch_id, int(time.time())),
    )


def batch_is_sealed(batch_id: str | None) -> bool:
    """True if this batch_id has been sealed. Never raises."""
    try:
        bid = _ok_batch_id(batch_id) or _text(batch_id)
        if not bid:
            return False
        with _lock:
            return _is_sealed_unlocked(bid)
    except Exception:
        return False


def seal_batch(batch_id: str | None) -> None:
    """Seal a batch so later record_probe writes are ignored. Never raises."""
    try:
        bid = _ok_batch_id(batch_id) or _text(batch_id)
        if not bid:
            return
        with _lock:
            conn = _connect()
            _seal_unlocked(bid)
            conn.commit()
            _chmod_db_files(_conn_path or db_path())
    except Exception:
        return


def _write_probe_row(dest: str, snap: dict, meta: dict) -> None:
    """Insert one probe + observations. Caller holds _lock. Does not commit."""
    ts = _as_int(snap.get("ts"), None)
    if ts is None:
        ts = int(time.time())
    live = 1 if snap.get("live") else 0
    pay_to = _payto(snap)
    amount = _observed_amount(snap)
    schema_present = _observed_schema_present(snap)
    payable = 1 if _observed_payable(snap) else 0
    if schema_present is not None:
        invocable = 1 if (payable and schema_present) else 0
        invocable_known = True
    elif live == 0:
        invocable = 0
        invocable_known = True
    else:
        invocable = 0
        invocable_known = False
    latency = _as_int(snap.get("latency_ms"), None)
    miss = _text(snap.get("miss_reason"))
    rail = _text(snap.get("rail"))
    http_status = _as_int(snap.get("status"), None)
    batch_id = _text(snap.get("batch_id"))
    obs_source = _text(snap.get("source")) or "402signal"
    claimed = _claimed_blob(snap)
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT last_payTo, last_amount, schema_present, payTo_changed_at, price_changed_at, schema_changed_at, last_success_402 FROM url_state WHERE url = ?", (dest,))
    row = cur.fetchone()
    prev_pay = _text(row[0]) if row else None
    prev_amt = _text(row[1]) if row else None
    prev_schema = row[2] if row else None
    pay_changed_at = row[3] if row else None
    price_changed_at = row[4] if row else None
    schema_changed_at = row[5] if row else None
    last_ok = row[6] if row else None
    if prev_pay and pay_to and not payment.payto_equal(prev_pay, pay_to, rail):
        pay_changed_at = ts
        meta["payTo_flipped"] = True
    claimed_pay = _text(claimed.get("payTo")) if claimed else None
    claimed_rail = _text(claimed.get("rail")) if claimed else None
    if claimed_pay and pay_to and not payment.payto_equal(
        claimed_pay, pay_to, claimed_rail or rail
    ):
        meta["payTo_flipped"] = True
    if prev_amt is not None and amount is not None and _price_flipped(prev_amt, amount, snap, rail):
        price_changed_at = ts
        meta["price_flipped"] = True
    if prev_schema is not None and schema_present is not None and int(prev_schema) != int(schema_present):
        schema_changed_at = ts
        meta["schema_flipped"] = True
    last_pay = pay_to if pay_to else prev_pay
    last_amt = amount if amount is not None else prev_amt
    last_schema = int(schema_present) if schema_present is not None else prev_schema
    if live:
        last_ok = ts
    cur.execute(
        "INSERT INTO probes (url, ts, live, payable, invocable, latency_ms, payTo, amount, miss_reason, rail, schema_present) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (dest, ts, live, payable, invocable, latency, pay_to, amount, miss, rail, schema_present),
    )
    probe_id = cur.lastrowid
    obs_fields = {
        "live": live,
        "payable": payable,
        "latency_ms": latency,
        "payTo": pay_to,
        "amount": amount,
        "http_status": http_status,
        "schema_present": schema_present,
    }
    if invocable_known:
        obs_fields["invocable"] = invocable
    _write_observed(
        cur,
        probe_id=probe_id,
        batch_id=batch_id,
        source=obs_source,
        rail=rail,
        url=dest,
        ts=ts,
        fields=obs_fields,
    )
    if claimed:
        _write_claimed(
            cur,
            probe_id=probe_id,
            batch_id=batch_id,
            source=claimed.get("source") or obs_source,
            rail=claimed.get("rail") or rail,
            url=dest,
            ts=ts,
            fields=claimed,
        )
    cur.execute(
        """
        INSERT INTO url_state (url, last_payTo, last_amount, schema_present, payTo_changed_at, price_changed_at, schema_changed_at, last_checked, last_success_402)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            last_payTo = excluded.last_payTo,
            last_amount = excluded.last_amount,
            schema_present = excluded.schema_present,
            payTo_changed_at = excluded.payTo_changed_at,
            price_changed_at = excluded.price_changed_at,
            schema_changed_at = excluded.schema_changed_at,
            last_checked = excluded.last_checked,
            last_success_402 = excluded.last_success_402
        """,
        (dest, last_pay, last_amt, last_schema, pay_changed_at, price_changed_at, schema_changed_at, ts, last_ok),
    )
    cur.execute(
        "SELECT id FROM probes WHERE url = ? ORDER BY ts DESC, id DESC",
        (dest,),
    )
    ids = [r[0] for r in cur.fetchall()]
    if len(ids) > PER_URL_CAP:
        _delete_probes_and_obs(cur, ids[PER_URL_CAP:])
    cur.execute("SELECT COUNT(*) FROM probes")
    n = int(cur.fetchone()[0] or 0)
    if n > GLOBAL_CAP:
        drop = n - GLOBAL_CAP
        cur.execute(
            "SELECT id FROM probes ORDER BY ts ASC, id ASC LIMIT ?",
            (drop,),
        )
        _delete_probes_and_obs(cur, [r[0] for r in cur.fetchall()])
    _cap_observations(cur, dest)


def _touch_shadow_verified(url: str | None, snap: dict | None = None) -> None:
    """Verification clock on catalog.sqlite. Fail closed. Never writes claims as observed."""
    dest = _text(url)
    if not dest:
        return
    try:
        from live402 import shadow

        shadow.mark_verified(dest)
        if isinstance(snap, dict) and snap.get("live"):
            shadow.touch_routed([dest])
    except Exception:
        return


def record_probe(url: str, snap: dict | None = None) -> dict:
    """Persist one probe. Ignores writes to a sealed batch_id. Never raises into the request path."""
    meta = {"payTo_flipped": False, "price_flipped": False, "schema_flipped": False}
    try:
        snap = snap if isinstance(snap, dict) else {}
        dest = _text(url) or _text(snap.get("url"))
        if not dest:
            return meta
        batch_id = _text(snap.get("batch_id"))
        with _lock:
            if batch_id and _is_sealed_unlocked(batch_id):
                return meta
            _write_probe_row(dest, snap, meta)
            conn = _connect()
            conn.commit()
            _chmod_db_files(_conn_path or db_path())
        _touch_shadow_verified(dest, snap)
        return meta
    except Exception:
        return meta


def persist_route_batch(batch_id: str | None, results: list | None) -> dict:
    """Coordinator: write accepted route observations, then seal. Never raises.

    Returns {url: write_meta} so the winner can be rehydrated with change state.
    Stragglers that later call record_probe with this batch_id are ignored.
    """
    metas: dict = {}
    try:
        bid = _ok_batch_id(batch_id)
        if not bid:
            return metas
        rows = []
        for raw in results or []:
            if not isinstance(raw, dict):
                continue
            dest = _text(raw.get("url"))
            if not dest:
                continue
            rows.append(raw)
        with _lock:
            if _is_sealed_unlocked(bid):
                return metas
            for raw in rows:
                snap = dict(raw)
                snap["batch_id"] = bid
                dest = _text(snap.get("url"))
                meta = {"payTo_flipped": False, "price_flipped": False, "schema_flipped": False}
                _write_probe_row(dest, snap, meta)
                metas[dest] = meta
                if meta.get("payTo_flipped"):
                    raw["payTo_changed"] = True
            _seal_unlocked(bid)
            conn = _connect()
            conn.commit()
            _chmod_db_files(_conn_path or db_path())
        for raw in rows:
            _touch_shadow_verified(_text(raw.get("url")), raw)
        return metas
    except Exception:
        return metas


def record_claim(url: str, fields: dict | None = None, *, source=None, rail=None, ts=None, batch_id=None) -> None:
    """Persist catalog-claimed fields. Never overwrites observed. Never touches url_state. Never raises."""
    try:
        dest = _text(url)
        blob = fields if isinstance(fields, dict) else {}
        if not dest or not blob:
            return
        when = _as_int(ts, None)
        if when is None:
            when = int(time.time())
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            _write_claimed(
                cur,
                probe_id=None,
                batch_id=_text(batch_id),
                source=source,
                rail=rail,
                url=dest,
                ts=when,
                fields=blob,
            )
            _cap_observations(cur, dest)
            conn.commit()
            _chmod_db_files(_conn_path or db_path())
    except Exception:
        return


def rank_hints(urls: list[str]) -> dict[str, dict]:
    """Cheap sqlite join for probe-shortlist order. Empty on miss. Never invents 0.0 rates."""
    dests: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        dest = _text(raw)
        if not dest or dest in seen:
            continue
        seen.add(dest)
        dests.append(dest)
    if not dests:
        return {}
    out: dict[str, dict] = {}
    try:
        now = int(time.time())
        cutoff = now - WEEK
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            qmarks = ",".join("?" * len(dests))
            cur.execute(
                f"SELECT url, last_checked, last_success_402 FROM url_state WHERE url IN ({qmarks})",
                dests,
            )
            for url, last_checked, last_ok in cur.fetchall():
                out[url] = {
                    "last_checked": last_checked,
                    "last_success_402": last_ok,
                    "n_7d": 0,
                    "ok_7d": 0,
                }
            cur.execute(
                f"SELECT url, COUNT(*), SUM(live) FROM probes WHERE url IN ({qmarks}) AND ts >= ? GROUP BY url",
                (*dests, cutoff),
            )
            for url, n, ok in cur.fetchall():
                row = out.setdefault(
                    url,
                    {
                        "last_checked": None,
                        "last_success_402": None,
                        "n_7d": 0,
                        "ok_7d": 0,
                    },
                )
                row["n_7d"] = int(n or 0)
                row["ok_7d"] = int(ok or 0)
        return out
    except Exception:
        return {}


def preview_observations(urls: list[str]) -> dict[str, dict]:
    """Read-only claimed/observed join for /preview. Never probes. Never invents rates."""
    dests: list[str] = []
    alias: dict[str, str] = {}
    for raw in urls:
        dest = _text(raw)
        if not dest:
            continue
        alias[raw] = dest
        if dest not in dests:
            dests.append(dest)
    unknown = {"status": "not_yet_observed"}
    out: dict[str, dict] = {raw: dict(unknown) for raw in alias}
    if not dests:
        return out
    try:
        hints = rank_hints(dests)
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            qmarks = ",".join("?" * len(dests))
            cur.execute(
                f"""
                SELECT url, field, value, ts
                FROM observations
                WHERE url IN ({qmarks}) AND source_type = ?
                ORDER BY ts DESC, id DESC
                """,
                (*dests, SOURCE_OBSERVED),
            )
            rows = cur.fetchall()
        latest: dict[str, dict] = {}
        for url, field, value, _ts in rows:
            bucket = latest.setdefault(url, {})
            if field in bucket:
                continue
            bucket[field] = value
        by_dest: dict[str, dict] = {}
        for dest in dests:
            hint = hints.get(dest) or {}
            fields = latest.get(dest) or {}
            last_checked = hint.get("last_checked")
            if last_checked is None and not fields:
                by_dest[dest] = dict(unknown)
                continue
            row = {"status": "observed", "n_7d": int(hint.get("n_7d") or 0)}
            iso = _iso_ts(last_checked)
            if iso:
                row["last_checked"] = iso
            payable = _as_int(fields.get("payable"), None)
            if payable is not None:
                row["payable"] = bool(payable)
            invocable = _as_int(fields.get("invocable"), None)
            if invocable is not None:
                row["invocable"] = bool(invocable)
            latency = _as_int(fields.get("latency_ms"), None)
            if latency is not None:
                row["last_latency_ms"] = latency
            n_7d = int(row["n_7d"] or 0)
            if n_7d >= MIN_HEALTHY_N:
                ok = int(hint.get("ok_7d") or 0)
                row["success_7d"] = (ok / n_7d) if n_7d else None
            by_dest[dest] = row
        for raw, dest in alias.items():
            out[raw] = dict(by_dest.get(dest) or unknown)
        return out
    except Exception:
        return {raw: dict(unknown) for raw in alias}


def latest_observations(url: str) -> dict:
    """Latest claimed vs observed per field. Missing side/field is empty / absent. Never invent 0/false."""
    out = {"claimed": {}, "observed": {}}
    dest = _text(url)
    if not dest:
        return out
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT source_type, field, value, source, rail, ts
                FROM observations
                WHERE url = ? AND source_type IN (?, ?)
                ORDER BY ts DESC, id DESC
                """,
                (dest, SOURCE_OBSERVED, SOURCE_CLAIMED),
            )
            rows = cur.fetchall()
        seen: set[tuple[str, str]] = set()
        for source_type, field, value, source, rail, ts in rows:
            if source_type == SOURCE_OBSERVED:
                side = "observed"
            elif source_type == SOURCE_CLAIMED:
                side = "claimed"
            else:
                continue
            key = (side, field)
            if key in seen:
                continue
            seen.add(key)
            out[side][field] = {
                "value": value,
                "source": source,
                "rail": rail,
                "ts": ts,
                "source_type": source_type,
                "provenance": source_type,
                "observed_at": ts if source_type == SOURCE_OBSERVED else None,
            }
        return out
    except Exception:
        return {"claimed": {}, "observed": {}}


def summary(url: str) -> dict:
    """Sourced history for one URL. Unknown rates are None, never 0.0."""
    out = _empty_summary()
    dest = _text(url)
    if not dest:
        return out
    try:
        now = int(time.time())
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT last_payTo, payTo_changed_at, price_changed_at, schema_changed_at, last_checked, last_success_402 FROM url_state WHERE url = ?",
                (dest,),
            )
            state = cur.fetchone()
            if state:
                out["last_payTo"] = state[0]
                out["payTo_changed_at"] = state[1]
                out["price_changed_at"] = state[2]
                out["schema_changed_at"] = state[3]
                out["last_checked"] = state[4]
                out["last_success_402"] = state[5]
            cur.execute(
                "SELECT ts, live, latency_ms FROM probes WHERE url = ? AND ts >= ?",
                (dest, now - WEEK),
            )
            rows = cur.fetchall()
        n_24h = 0
        ok_24h = 0
        n_7d = 0
        ok_7d = 0
        latencies: list[int] = []
        cutoff_24h = now - DAY
        for ts, live, latency in rows:
            n_7d += 1
            if live:
                ok_7d += 1
            if ts is not None and int(ts) >= cutoff_24h:
                n_24h += 1
                if live:
                    ok_24h += 1
            if latency is not None:
                try:
                    latencies.append(int(latency))
                except (TypeError, ValueError):
                    pass
        out["n_24h"] = n_24h
        out["ok_24h"] = ok_24h
        out["n_7d"] = n_7d
        out["ok_7d"] = ok_7d
        out["success_24h"] = (ok_24h / n_24h) if n_24h else None
        out["success_7d"] = (ok_7d / n_7d) if n_7d else None
        out["p50_latency_ms"] = _percentile(latencies, 50)
        out["p95_latency_ms"] = _percentile(latencies, 95)
        return out
    except Exception:
        return _empty_summary()


def _has_schema(result: dict) -> bool:
    target = result.get("target") if isinstance(result.get("target"), dict) else {}
    schema = target.get("inputSchema")
    if isinstance(schema, dict) and (schema.get("properties") or schema.get("required")):
        return True
    if result.get("schema_source"):
        return True
    return False


def compute_readiness(result: dict, n_7d: int = 0) -> str:
    """discovered | payable | invocable | recently_verified. Never fake healthy."""
    _ = n_7d
    live = bool(result.get("live"))
    if "payable" in result and result.get("payable") is not None:
        payable = bool(result.get("payable"))
    else:
        payable = _observed_payable(result) if live else False
    this_probe = result.get("verified_seconds_ago", 0) == 0
    if payable and _has_schema(result):
        return "invocable"
    if payable and this_probe:
        # This request's probe succeeded as payable. Emit recently_verified
        # (freshness) which is still a payable outcome; callers may treat it as payable.
        return "recently_verified"
    if payable:
        return "payable"
    return "discovered"


def _as_side_value(row: dict | None, *, as_int: bool = False):
    if not row:
        return None
    val = row.get("value")
    if val is None or val == "":
        return None
    if as_int:
        try:
            return int(val)
        except (TypeError, ValueError):
            return None
    return val


def _empty_claimed() -> dict:
    return {
        "payTo": None,
        "amount": None,
        "schema_present": None,
        "facilitator": None,
        "claimed_at": None,
        "payment_options": None,
        "accepts": None,
    }


def _empty_observed() -> dict:
    return {
        "http_status": None,
        "payTo": None,
        "amount": None,
        "latency_ms": None,
        "schema_present": None,
        "payable": None,
        "invocable": None,
        "observed_at": None,
    }


def _side_ts(rows: dict) -> int | None:
    latest = None
    for row in (rows or {}).values():
        if not isinstance(row, dict):
            continue
        t = row.get("ts")
        if t is None:
            continue
        try:
            n = int(t)
        except (TypeError, ValueError):
            continue
        if latest is None or n > latest:
            latest = n
    return latest


def attach_to_result(result: dict | None, meta: dict | None = None) -> dict:
    """Attach freshness, readiness, risk, history. Backward compatible. Never raises."""
    if not isinstance(result, dict):
        return {}
    try:
        meta = meta if isinstance(meta, dict) else {}
        probed_at = result.get("probed_at")
        result["verified_at"] = probed_at
        result["verified_seconds_ago"] = 0
        if meta.get("payTo_flipped"):
            result["payTo_changed"] = True
        url = _text(result.get("url")) or ""
        summ = summary(url) if url else _empty_summary()
        n_7d = int(summ.get("n_7d") or 0)
        result["readiness"] = compute_readiness(result, n_7d)
        # Never emit readiness=healthy unless n_7d >= 10. Prefer unknown.
        result["readiness_healthy"] = None
        latest = latest_observations(url) if url else {"claimed": {}, "observed": {}}
        claimed_rows = latest.get("claimed") or {}
        observed_rows = latest.get("observed") or {}
        prior_claimed = result.get("claimed") if isinstance(result.get("claimed"), dict) else {}
        claimed = _empty_claimed()
        claimed["payTo"] = _as_side_value(claimed_rows.get("payTo"))
        if claimed["payTo"] is None:
            claimed["payTo"] = prior_claimed.get("payTo")
        claimed["amount"] = _as_side_value(claimed_rows.get("amount"))
        if claimed["amount"] is None:
            claimed["amount"] = prior_claimed.get("amount")
        claimed["schema_present"] = _as_side_value(claimed_rows.get("schema_present"), as_int=True)
        if claimed["schema_present"] is None:
            claimed["schema_present"] = prior_claimed.get("schema_present")
        claimed["facilitator"] = _as_side_value(claimed_rows.get("facilitator"))
        if claimed["facilitator"] is None:
            claimed["facilitator"] = prior_claimed.get("facilitator")
        claimed["claimed_at"] = _side_ts(claimed_rows)
        if prior_claimed.get("payment_options"):
            claimed["payment_options"] = prior_claimed["payment_options"]
        if prior_claimed.get("accepts"):
            claimed["accepts"] = prior_claimed["accepts"]
        observed = _empty_observed()
        observed["http_status"] = _as_side_value(observed_rows.get("http_status"), as_int=True)
        observed["payTo"] = _as_side_value(observed_rows.get("payTo"))
        observed["amount"] = _as_side_value(observed_rows.get("amount"))
        observed["latency_ms"] = _as_side_value(observed_rows.get("latency_ms"), as_int=True)
        observed["schema_present"] = _as_side_value(observed_rows.get("schema_present"), as_int=True)
        observed["payable"] = _as_side_value(observed_rows.get("payable"), as_int=True)
        observed["invocable"] = _as_side_value(observed_rows.get("invocable"), as_int=True)
        observed["observed_at"] = _side_ts(observed_rows)
        result["claimed"] = claimed
        result["observed"] = observed
        obs_pay = observed.get("payTo") or _text(result.get("payTo"))
        cl_pay = claimed.get("payTo")
        rail = _text(result.get("rail"))
        if obs_pay and cl_pay and not payment.payto_equal(obs_pay, cl_pay, rail):
            result["payTo_changed"] = True
        if result.get("payTo_changed"):
            result["risk"] = ["payTo_changed"]
        changes = {}
        for key in ("payTo_changed_at", "price_changed_at", "schema_changed_at"):
            val = summ.get(key)
            if val is not None:
                iso = _iso_ts(val)
                changes[key] = iso if iso else val
        if changes:
            result["changes"] = changes
        result["history"] = {
            "success_24h": summ.get("success_24h"),
            "success_7d": summ.get("success_7d"),
            "n_24h": summ.get("n_24h"),
            "n_7d": summ.get("n_7d"),
            "p50_latency_ms": summ.get("p50_latency_ms"),
            "p95_latency_ms": summ.get("p95_latency_ms"),
        }
        try:
            from live402 import reputation as reputation_mod

            reputation_mod.attach(result)
        except Exception:
            pass
        return result
    except Exception:
        result.setdefault("verified_at", result.get("probed_at"))
        result.setdefault("verified_seconds_ago", 0)
        result.setdefault("readiness", "discovered")
        result.setdefault("claimed", _empty_claimed())
        result.setdefault("observed", _empty_observed())
        result.setdefault(
            "history",
            {
                "success_24h": None,
                "success_7d": None,
                "n_24h": 0,
                "n_7d": 0,
                "p50_latency_ms": None,
                "p95_latency_ms": None,
            },
        )
        return result



def _empty_reputation_evidence() -> dict:
    return {
        "n_7d": None,
        "ok_7d": None,
        "success_7d": None,
        "probe_count_7d": None,
        "distinct_days_7d": None,
        "outcome_flips_7d": None,
        "first_probe_ts": None,
        "last_checked": None,
        "last_success_402": None,
        "age_s": None,
        "has_probe_history": False,
        "payTo_changed_at": None,
        "price_changed_at": None,
        "schema_changed_at": None,
        "rail_changed_at": None,
        "payTo_change_count": None,
        "price_change_count": None,
        "schema_change_count": None,
        "rail_change_count": None,
        "rails_observed": None,
    }


def reputation_evidence(url: str) -> dict:
    """Raw facts for reputation components. Missing stays None. Never invent 0.0 rates."""
    out = _empty_reputation_evidence()
    dest = _text(url)
    if not dest:
        return out
    try:
        now = int(time.time())
        cutoff = now - WEEK
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT last_payTo, payTo_changed_at, price_changed_at, schema_changed_at, last_checked, last_success_402 FROM url_state WHERE url = ?",
                (dest,),
            )
            state = cur.fetchone()
            if state:
                out["payTo_changed_at"] = state[1]
                out["price_changed_at"] = state[2]
                out["schema_changed_at"] = state[3]
                out["last_checked"] = state[4]
                out["last_success_402"] = state[5]
                if state[4] is not None:
                    out["age_s"] = max(0, now - int(state[4]))
            cur.execute(
                "SELECT ts, live, rail, payTo, amount, schema_present FROM probes WHERE url = ? ORDER BY ts ASC, id ASC",
                (dest,),
            )
            rows = cur.fetchall()
        if not rows:
            return out
        out["has_probe_history"] = True
        out["first_probe_ts"] = rows[0][0]
        n_7d = 0
        ok_7d = 0
        days = set()
        flips = 0
        prev_live = None
        prev_pay = None
        prev_amt = None
        prev_schema = None
        prev_rail = None
        pay_changes = 0
        price_changes = 0
        schema_changes = 0
        rail_changes = 0
        rail_changed_at = None
        rails = []
        for ts, live, rail, pay_to, amount, schema_present in rows:
            rname = _text(rail)
            if rname and rname not in rails:
                rails.append(rname)
            if prev_pay is not None and pay_to and not payment.payto_equal(prev_pay, pay_to, rname):
                pay_changes += 1
            if prev_amt is not None and amount is not None and str(prev_amt) != str(amount):
                price_changes += 1
            if prev_schema is not None and schema_present is not None and int(prev_schema) != int(schema_present):
                schema_changes += 1
            if prev_rail is not None and rname and prev_rail != rname:
                rail_changes += 1
                rail_changed_at = ts
            if pay_to:
                prev_pay = pay_to
            if amount is not None:
                prev_amt = amount
            if schema_present is not None:
                prev_schema = schema_present
            if rname:
                prev_rail = rname
            if ts is None or int(ts) < cutoff:
                prev_live = live
                continue
            n_7d += 1
            if live:
                ok_7d += 1
            try:
                days.add(int(ts) // DAY)
            except (TypeError, ValueError):
                pass
            if prev_live is not None and int(prev_live or 0) != int(live or 0):
                flips += 1
            prev_live = live
        out["n_7d"] = n_7d
        out["ok_7d"] = ok_7d
        out["probe_count_7d"] = n_7d
        out["success_7d"] = (ok_7d / n_7d) if n_7d else None
        out["distinct_days_7d"] = len(days) if n_7d else 0
        out["outcome_flips_7d"] = flips if n_7d else 0
        out["payTo_change_count"] = pay_changes
        out["price_change_count"] = price_changes
        out["schema_change_count"] = schema_changes
        out["rail_change_count"] = rail_changes
        out["rail_changed_at"] = rail_changed_at
        if rails:
            out["rails_observed"] = rails
        return out
    except Exception:
        return _empty_reputation_evidence()


def ensure_scoring_model(record: dict | None) -> dict | None:
    """Append-only scoring-model log. Never updates a prior hash. Never raises."""
    try:
        rec = record if isinstance(record, dict) else {}
        model_id = _text(rec.get("model_id"))
        digest = _text(rec.get("model_hash"))
        spec = rec.get("spec_json")
        if not model_id or not digest or not spec:
            return None
        effective = _as_int(rec.get("effective_ts"), None)
        if effective is None:
            effective = int(time.time())
        with _lock:
            conn = _connect()
            conn.execute(
                """
                INSERT OR IGNORE INTO scoring_models
                    (model_id, model_hash, effective_ts, spec_json, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (model_id, digest, int(effective), str(spec), int(time.time())),
            )
            conn.commit()
            _chmod_db_files(_conn_path or db_path())
        return {"model_id": model_id, "model_hash": digest, "effective_ts": int(effective)}
    except Exception:
        return None


def scoring_model(model_id: str | None = None) -> dict | None:
    """Latest logged model, or a specific id. None if the log is empty."""
    try:
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            if model_id:
                cur.execute(
                    """
                    SELECT model_id, model_hash, effective_ts, spec_json, recorded_at
                    FROM scoring_models WHERE model_id = ?
                    ORDER BY recorded_at DESC LIMIT 1
                    """,
                    (model_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT model_id, model_hash, effective_ts, spec_json, recorded_at
                    FROM scoring_models ORDER BY recorded_at DESC LIMIT 1
                    """
                )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "model_id": row[0],
            "model_hash": row[1],
            "effective_ts": row[2],
            "spec_json": row[3],
            "recorded_at": row[4],
        }
    except Exception:
        return None


def _iso_ts(ts) -> str | None:
    try:
        n = int(ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(n, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def pulse_observed() -> dict:
    """7d snapshot from 402signal_observed only. Facts, never a binary healthy/ENR."""
    out = {"n_7d": 0, "reliability": "unknown", "source": SOURCE_OBSERVED}
    try:
        now = int(time.time())
        cutoff = now - WEEK
        with _lock:
            conn = _connect()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT field, value, COUNT(*)
                FROM observations
                WHERE source_type = ? AND ts >= ? AND field IN ('live', 'payable', 'invocable')
                GROUP BY field, value
                """,
                (SOURCE_OBSERVED, cutoff),
            )
            rows = cur.fetchall()
        n_live = 0
        ok_live = 0
        n_pay = 0
        ok_pay = 0
        n_inv = 0
        ok_inv = 0
        for field, value, count in rows:
            n = int(count or 0)
            is_one = str(value) in {"1", "true", "True"}
            if field == "live":
                n_live += n
                if is_one:
                    ok_live += n
            elif field == "payable":
                n_pay += n
                if is_one:
                    ok_pay += n
            elif field == "invocable":
                n_inv += n
                if is_one:
                    ok_inv += n
        n_7d = n_live
        out["n_7d"] = n_7d
        if n_7d < MIN_HEALTHY_N:
            out["reliability"] = "unknown"
            return out
        out["success_7d"] = (ok_live / n_7d) if n_7d else None
        if n_pay >= MIN_HEALTHY_N:
            out["payable_rate_7d"] = ok_pay / n_pay
        if n_inv >= MIN_HEALTHY_N:
            out["invocable_rate_7d"] = ok_inv / n_inv
        out.pop("reliability", None)
        return out
    except Exception:
        return {"n_7d": 0, "reliability": "unknown", "source": SOURCE_OBSERVED}


def _ok_batch_id(raw) -> str | None:
    text = _text(raw)
    if not text or len(text) > 128:
        return None
    for ch in text:
        if not (ch.isalnum() or ch in "-_"):
            return None
    return text


def canonical_observation_rows(rows: list[dict]) -> str:
    """Stable JSON for hashing. Public fields only. No signatures/headers/envelopes/keys."""
    slim: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = _text(row.get("url"))
        field = _text(row.get("field"))
        if not url or not field:
            continue
        ts = _as_int(row.get("ts"), None)
        if ts is None:
            continue
        value = row.get("value")
        if value is None:
            continue
        slim.append(
            {
                "field": field,
                "ts": int(ts),
                "url": url,
                "value": str(value),
            }
        )
    slim.sort(key=lambda r: (r["url"], r["field"], r["ts"], r["value"]))
    return json.dumps(slim, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def hash_canonical(canonical: str) -> str:
    return hashlib.sha256((canonical or "").encode("utf-8")).hexdigest()


def _batch_rows_unlocked(batch_id: str) -> list[dict]:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT url, field, value, ts
        FROM observations
        WHERE batch_id = ? AND source_type = ?
        ORDER BY url, field, ts, id
        """,
        (batch_id, SOURCE_OBSERVED),
    )
    out = []
    for url, field, value, ts in cur.fetchall():
        out.append({"url": url, "field": field, "value": value, "ts": ts})
    return out


def _batch_rows(batch_id: str) -> list[dict]:
    with _lock:
        return _batch_rows_unlocked(batch_id)


def _latest_batch_id_unlocked() -> str | None:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT batch_id, MAX(ts) AS latest
        FROM observations
        WHERE source_type = ? AND batch_id IS NOT NULL AND batch_id != ''
        GROUP BY batch_id
        ORDER BY latest DESC, batch_id DESC
        LIMIT 1
        """,
        (SOURCE_OBSERVED,),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    return _ok_batch_id(row[0])


def latest_batch_id() -> str | None:
    try:
        with _lock:
            return _latest_batch_id_unlocked()
    except Exception:
        return None


def attestation_for(batch_id: str | None = None) -> dict | None:
    """Public hash of a 402signal_observed probe batch. Fail closed. Never on-chain.

    Computing the hash seals the batch so later writes cannot change it.
    """
    try:
        with _lock:
            bid = _ok_batch_id(batch_id) if batch_id else _latest_batch_id_unlocked()
            if not bid:
                return None
            rows = _batch_rows_unlocked(bid)
            if not rows:
                return None
            canonical = canonical_observation_rows(rows)
            digest = hash_canonical(canonical)
            created = _iso_ts(min(int(r["ts"]) for r in rows if r.get("ts") is not None))
            _seal_unlocked(bid)
            conn = _connect()
            conn.commit()
            _chmod_db_files(_conn_path or db_path())
        return {
            "batch_id": bid,
            "created_at": created,
            "n": len(rows),
            "algo": ATTEST_ALGO,
            "hash": digest,
        }
    except Exception:
        return None
