"""Probe observation history. Stdlib sqlite3 only. Fail closed. Never pay."""

from __future__ import annotations

import math
import os
import sqlite3
import threading
import time

DEFAULT_DB = "/tmp/live402-history.sqlite"
VOLUME_DB = "/data/live402-history.sqlite"
PER_URL_CAP = 500
GLOBAL_CAP = 50_000
DAY = 86400
WEEK = 7 * DAY

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


def _amount(snap: dict) -> str | None:
    if snap.get("amount") is not None and snap.get("amount") != "":
        return _text(snap.get("amount"))
    target = snap.get("target") if isinstance(snap.get("target"), dict) else {}
    if target.get("amountAtomic") is not None and target.get("amountAtomic") != "":
        return _text(target.get("amountAtomic"))
    env = snap.get("envelope") if isinstance(snap.get("envelope"), dict) else {}
    accepts = env.get("accepts") if isinstance(env, dict) else None
    if isinstance(accepts, list):
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


def _schema_present(snap: dict) -> int:
    target = snap.get("target") if isinstance(snap.get("target"), dict) else {}
    schema = target.get("inputSchema")
    if isinstance(schema, dict) and (schema.get("properties") or schema.get("required") or schema.get("type")):
        return 1
    if snap.get("schema_source"):
        return 1
    if snap.get("schema_present") is not None:
        return 1 if snap.get("schema_present") else 0
    return 1 if snap.get("invocable") else 0


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


def record_probe(url: str, snap: dict | None = None) -> dict:
    """Persist one probe. Never raises into the request path."""
    meta = {"payTo_flipped": False, "price_flipped": False, "schema_flipped": False}
    try:
        snap = snap if isinstance(snap, dict) else {}
        dest = _text(url) or _text(snap.get("url"))
        if not dest:
            return meta
        ts = _as_int(snap.get("ts"), None)
        if ts is None:
            ts = int(time.time())
        live = 1 if snap.get("live") else 0
        pay_to = _payto(snap)
        amount = _amount(snap)
        schema_present = _schema_present(snap)
        payable = 1 if (live and pay_to) else 0
        invocable = 1 if (payable and schema_present) else 0
        latency = _as_int(snap.get("latency_ms"), None)
        miss = _text(snap.get("miss_reason"))
        rail = _text(snap.get("rail"))
        with _lock:
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
            if prev_pay and pay_to and prev_pay.lower() != pay_to.lower():
                pay_changed_at = ts
                meta["payTo_flipped"] = True
            if prev_amt is not None and amount is not None and str(prev_amt) != str(amount):
                price_changed_at = ts
                meta["price_flipped"] = True
            if prev_schema is not None and int(prev_schema or 0) != int(schema_present):
                schema_changed_at = ts
                meta["schema_flipped"] = True
            last_pay = pay_to if pay_to else prev_pay
            last_amt = amount if amount is not None else prev_amt
            last_schema = int(schema_present)
            if live:
                last_ok = ts
            cur.execute(
                "INSERT INTO probes (url, ts, live, payable, invocable, latency_ms, payTo, amount, miss_reason, rail, schema_present) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (dest, ts, live, payable, invocable, latency, pay_to, amount, miss, rail, schema_present),
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
                extra = [(i,) for i in ids[PER_URL_CAP:]]
                cur.executemany("DELETE FROM probes WHERE id = ?", extra)
            cur.execute("SELECT COUNT(*) FROM probes")
            n = int(cur.fetchone()[0] or 0)
            if n > GLOBAL_CAP:
                drop = n - GLOBAL_CAP
                cur.execute(
                    "DELETE FROM probes WHERE id IN (SELECT id FROM probes ORDER BY ts ASC, id ASC LIMIT ?)",
                    (drop,),
                )
            conn.commit()
            _chmod_db_files(_conn_path or db_path())
        return meta
    except Exception:
        return meta


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
    pay_to = _text(result.get("payTo"))
    live = bool(result.get("live"))
    payable = bool(live and pay_to)
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
        if result.get("payTo_changed"):
            result["risk"] = ["payTo_changed"]
        result["history"] = {
            "success_24h": summ.get("success_24h"),
            "success_7d": summ.get("success_7d"),
            "n_24h": summ.get("n_24h"),
            "n_7d": summ.get("n_7d"),
            "p50_latency_ms": summ.get("p50_latency_ms"),
            "p95_latency_ms": summ.get("p95_latency_ms"),
        }
        return result
    except Exception:
        result.setdefault("verified_at", result.get("probed_at"))
        result.setdefault("verified_seconds_ago", 0)
        result.setdefault("readiness", "discovered")
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
