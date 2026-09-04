"""Single-machine payment-authorization replay guard (SEC-ROUTER-001).

In-memory maps cover same-process inflight waiters and a 120s response
cache. SHA-256(fingerprint) plus settle outcome persist in sqlite with a
UNIQUE constraint so restart, TTL expiry, and a second process cannot
settle the same authorization again.

Outcome states: settlement_pending and unknown are non-terminal. They
fail closed and never authorize a second economic action. settled,
not_settled, and rejected are terminal and may replay a stored HTTP result.

Never persist raw payment material. Single-machine until a shared
ledger exists. This is not facilitator exactly-once.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time

from live402 import clock, payment

COMPLETED_TTL_SECONDS = 120.0
MAX_COMPLETED = 2048
WAIT_SLICE = 0.05

DEFAULT_DB = "/tmp/live402-replay.sqlite"
VOLUME_DB = "/data/live402-replay.sqlite"

STATE_PENDING = "settlement_pending"
STATE_UNKNOWN = "unknown"
STATE_SETTLED = "settled"
STATE_NOT_SETTLED = "not_settled"
STATE_REJECTED = "rejected"
NON_TERMINAL_STATES = frozenset({STATE_PENDING, STATE_UNKNOWN})
TERMINAL_STATES = frozenset({STATE_SETTLED, STATE_NOT_SETTLED, STATE_REJECTED})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settle_ledger (
    fp_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    outcome_json TEXT,
    created_at REAL NOT NULL,
    CONSTRAINT settle_fp_hash_unique UNIQUE (fp_hash)
);
"""


def canonical_fingerprint(payload: dict, accept: dict) -> str:
    """SHA-256 of canonical payload + matched rail identity. Never log the input."""
    req = payment.official_requirements(accept if isinstance(accept, dict) else {})
    rail = payment.rail_of_accept(accept if isinstance(accept, dict) else {})
    material = {
        "payload": payload if isinstance(payload, dict) else {},
        "rail": rail,
        "network": req.get("network"),
        "asset": req.get("asset"),
        "amount": str(req.get("amount") or ""),
        "payTo": req.get("payTo"),
        "scheme": req.get("scheme"),
    }
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def durable_hash(fp: str) -> str:
    """SHA-256(fingerprint). This is what sqlite stores. Never the fingerprint."""
    return hashlib.sha256(str(fp).encode("ascii")).hexdigest()


class _Entry:
    __slots__ = ("event", "result")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: tuple | None = None


_lock = threading.Lock()
_inflight: dict[str, _Entry] = {}
_completed: dict[str, tuple[float, tuple]] = {}
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None


def db_path() -> str:
    raw = (os.environ.get("LIVE402_REPLAY_DB") or "").strip()
    if raw:
        return raw
    try:
        if os.path.isdir("/data") and os.access("/data", os.W_OK):
            return VOLUME_DB
    except Exception:
        pass
    return DEFAULT_DB


def _chmod_db_files(path: str) -> None:
    for p in (path, path + "-wal", path + "-shm"):
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


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
    # This ledger gates a second economic action after process/host failure.
    # FULL is required: NORMAL may lose the most recent WAL commit on power
    # loss even though process-crash tests pass.
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript(_SCHEMA)
    conn.commit()
    _migrate_columns(conn)
    _conn = conn
    _conn_path = path
    _chmod_db_files(path)
    return conn


def _test_support() -> bool:
    return any(
        (os.environ.get(name) or "").strip() == "1"
        for name in ("LIVE402_FIXTURE", "LIVE402_PQ_TEST_SUPPORT")
    )


def durable_ready() -> bool:
    """True when the paid-settlement ledger is writable and durable.

    Production must use the one mounted `/data` ledger configured in
    `fly.toml`; silently falling back to `/tmp` would reopen settled payment
    authorizations after a Machine restart. Tests may use isolated temp DBs.
    """
    path = db_path()
    if not _test_support():
        try:
            if os.path.realpath(path) != os.path.realpath(VOLUME_DB):
                return False
        except (OSError, TypeError, ValueError):
            return False
    with _lock:
        conn = None
        try:
            conn = _connect()
            sync = conn.execute("PRAGMA synchronous").fetchone()
            journal = conn.execute("PRAGMA journal_mode").fetchone()
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settle_ledger'"
            ).fetchone()
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
            return bool(
                sync
                and int(sync[0]) == 2
                and journal
                and str(journal[0]).lower() == "wal"
                and table
            )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            try:
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass
            return False


def _migrate_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settle_ledger)").fetchall()}
    if "state" not in cols:
        conn.execute(
            "ALTER TABLE settle_ledger ADD COLUMN state TEXT NOT NULL DEFAULT 'settlement_pending'"
        )
        conn.execute(
            """
            UPDATE settle_ledger SET state = CASE
                WHEN outcome_json IS NOT NULL AND outcome_json != '' THEN ?
                ELSE ?
            END
            """,
            (STATE_SETTLED, STATE_UNKNOWN),
        )
        conn.commit()


def _close_conn_locked() -> None:
    global _conn, _conn_path
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = None
    _conn_path = None


def _clear_memory_locked() -> None:
    for entry in _inflight.values():
        entry.event.set()
    _inflight.clear()
    _completed.clear()


def reset() -> None:
    """Drop memory and the sqlite file (tests)."""
    with _lock:
        _clear_memory_locked()
        path = _conn_path or db_path()
        _close_conn_locked()
        for p in (path, path + "-wal", path + "-shm"):
            try:
                os.remove(p)
            except OSError:
                pass


def reset_memory() -> None:
    """Drop process-local maps only. Sqlite stays. Tests simulate restart."""
    with _lock:
        _clear_memory_locked()
        _close_conn_locked()


def _encode_outcome(result: tuple) -> str:
    code, body, extra = result
    return json.dumps({"c": code, "b": body, "e": extra}, separators=(",", ":"), default=str)


def _decode_outcome(raw: str) -> tuple | None:
    try:
        data = json.loads(raw)
        code = int(data["c"])
        body = data["b"]
        extra = data["e"]
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    if extra is not None and not isinstance(extra, dict):
        return None
    return code, body, extra


def _explicit_settlement_state(result: tuple) -> bool | None:
    """Read a valid success-only terminal outcome; legacy rows return None."""
    try:
        code, body, _extra = result
    except (TypeError, ValueError):
        return None
    if code not in (200, 503) or not isinstance(body, dict):
        return None
    billing = body.get("billing")
    if not isinstance(billing, dict):
        return None
    if billing.get("model") != payment.ROUTING_BILLING_MODEL:
        return None
    if billing.get("condition") != payment.ROUTING_SETTLEMENT_CONDITION:
        return None
    if billing.get("asset") != "USDC":
        return None
    if billing.get("amount_atomic") != payment.AMOUNT_ATOMIC:
        return None
    if billing.get("display_amount") != payment.AMOUNT_USD:
        return None
    if billing.get("rail") not in payment.SUPPORTED_RAILS:
        return None
    attempted = billing.get("settlement_attempted")
    settled = billing.get("settled")
    if type(attempted) is not bool or type(settled) is not bool:
        return None
    if settled and not attempted:
        return None
    if not settled and attempted:
        return None
    if settled and code == 200 and body.get("live") is not True:
        return None
    if not settled and (code != 503 or body.get("live") is not False):
        return None
    return settled


def _ledger_lookup(fp_hash: str) -> tuple[str, tuple | None]:
    """Read a durable row. missing / cached / reject. Fail closed on sqlite errors.

    Non-terminal states (settlement_pending, unknown) never replay a
    success and never authorize a second settle.
    """
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT state, outcome_json FROM settle_ledger WHERE fp_hash = ?",
            (fp_hash,),
        ).fetchone()
    except sqlite3.Error:
        return "reject", None
    if row is None:
        return "missing", None
    state, outcome = row
    if state in NON_TERMINAL_STATES or state not in TERMINAL_STATES:
        return "reject", None
    if outcome:
        decoded = _decode_outcome(outcome)
        if decoded is not None:
            return "cached", decoded
    return "reject", None


def _ledger_reserve(fp_hash: str) -> str:
    """INSERT settlement_pending. run or reject. UNIQUE is the inter-process lock."""
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO settle_ledger (fp_hash, state, outcome_json, created_at) VALUES (?, ?, NULL, ?)",
            (fp_hash, STATE_PENDING, time.time()),
        )
        conn.commit()
        return "run"
    except sqlite3.IntegrityError:
        try:
            conn.rollback()
        except Exception:
            pass
        return "reject"
    except sqlite3.Error:
        return "reject"


def _ledger_finish(fp_hash: str, result: tuple, cache: bool) -> None:
    try:
        conn = _connect()
        if cache:
            explicit = _explicit_settlement_state(result)
            if explicit is True:
                state = STATE_SETTLED
            elif explicit is False:
                state = STATE_NOT_SETTLED
            else:
                # Backward compatibility for outcomes stored before this model.
                state = STATE_SETTLED if result[0] in (200, 503) else STATE_REJECTED
            conn.execute(
                "UPDATE settle_ledger SET state = ?, outcome_json = ? WHERE fp_hash = ?",
                (state, _encode_outcome(result), fp_hash),
            )
        else:
            conn.execute("DELETE FROM settle_ledger WHERE fp_hash = ?", (fp_hash,))
        conn.commit()
    except sqlite3.Error:
        pass


def _ledger_mark_unknown(fp_hash: str) -> None:
    """Abandon stays non-terminal. Do not delete: no second economic action."""
    try:
        conn = _connect()
        conn.execute(
            "UPDATE settle_ledger SET state = ? WHERE fp_hash = ? AND state IN (?, ?)",
            (STATE_UNKNOWN, fp_hash, STATE_PENDING, STATE_UNKNOWN),
        )
        conn.commit()
    except sqlite3.Error:
        pass


def _prune_completed(now: float) -> None:
    # TTL is the in-memory response cache only. Sqlite uniqueness does not expire.
    stale = [key for key, (exp, _res) in _completed.items() if exp <= now]
    for key in stale:
        _completed.pop(key, None)
    while len(_completed) > MAX_COMPLETED:
        oldest = next(iter(_completed))
        _completed.pop(oldest, None)


def peek_completed(fp: str) -> tuple | None:
    now = clock.monotonic()
    with _lock:
        _prune_completed(now)
        hit = _completed.get(fp)
        if not hit:
            return None
        exp, result = hit
        if exp <= now:
            _completed.pop(fp, None)
            return None
        return result


def begin(fp: str) -> tuple[str, _Entry | tuple | None]:
    """Acquire execution, return a cached result, wait, or reject a duplicate.

    A second process that hits the UNIQUE row is rejected (fail closed).
    """
    now = clock.monotonic()
    fp_hash = durable_hash(fp)
    with _lock:
        _prune_completed(now)
        cached = _completed.get(fp)
        if cached and cached[0] > now:
            return "cached", cached[1]
        existing = _inflight.get(fp)
        if existing is not None:
            return "wait", existing
        status, persisted = _ledger_lookup(fp_hash)
        if status == "cached":
            return "cached", persisted
        if status == "reject":
            return "reject", None
        if _ledger_reserve(fp_hash) != "run":
            return "reject", None
        entry = _Entry()
        _inflight[fp] = entry
        return "run", entry


def wait_result(entry: _Entry, deadline: float | None) -> tuple | None:
    """Wait for the in-flight owner. None means fail closed."""
    while True:
        left = None
        if deadline is not None:
            left = float(deadline) - clock.monotonic()
            if left <= 0:
                return entry.result
        wait = WAIT_SLICE if left is None else min(WAIT_SLICE, max(0.0, left))
        if entry.event.wait(timeout=wait):
            return entry.result
        if left is not None and left <= 0:
            return entry.result


def finish(fp: str, result: tuple, cache: bool) -> None:
    """Publish the result to waiters. Cache settled/rejected fingerprints only.

    cache=True writes the outcome to sqlite. cache=False (400) drops the
    reservation so a corrected body may retry. TTL still applies to RAM only.
    """
    now = clock.monotonic()
    fp_hash = durable_hash(fp)
    with _lock:
        entry = _inflight.get(fp)
        if entry is not None:
            entry.result = result
            entry.event.set()
            _inflight.pop(fp, None)
        if cache:
            _prune_completed(now)
            _completed[fp] = (now + COMPLETED_TTL_SECONDS, result)
            _prune_completed(now)
        _ledger_finish(fp_hash, result, cache)


def abandon(fp: str) -> None:
    """Release in-flight as unknown. Waiters fail closed. Row stays unique."""
    fp_hash = durable_hash(fp)
    with _lock:
        entry = _inflight.pop(fp, None)
        if entry is not None:
            entry.event.set()
        _ledger_mark_unknown(fp_hash)


def ledger_state(fp: str) -> str | None:
    """Persisted settle state for this fingerprint hash. None if missing."""
    fp_hash = durable_hash(fp)
    with _lock:
        try:
            conn = _connect()
            row = conn.execute(
                "SELECT state FROM settle_ledger WHERE fp_hash = ?",
                (fp_hash,),
            ).fetchone()
        except sqlite3.Error:
            return STATE_UNKNOWN
    if row is None:
        return None
    return str(row[0])
