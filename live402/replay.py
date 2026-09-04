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

import base64
import hashlib
import json
import os
import re
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
    fingerprint_version INTEGER NOT NULL DEFAULT 2,
    scope_hash TEXT,
    CONSTRAINT settle_fp_hash_unique UNIQUE (fp_hash)
);
CREATE TABLE IF NOT EXISTS replay_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

FINGERPRINT_VERSION = 2
CUTOVER_ACK_ENV = "LIVE402_REPLAY_V2_CUTOVER_ACK"
CUTOVER_ACK_VALUE = "payto-rotated-or-legacy-authorizations-expired"
_CUTOVER_META_KEY = "economic_fingerprint_v2_cutover"
MAX_SOLANA_TRANSACTION_BYTES = 1232
MAX_ALGORAND_GROUP = 16
MAX_INNER_B64_TEXT = 16 * 1024
_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_EVM_BYTES32_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _uint_text(value, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str) or not value or not value.isascii():
        raise ValueError("invalid %s" % name)
    if not value.isdigit() or len(value) > 78:
        raise ValueError("invalid %s" % name)
    number = int(value)
    if number < 0 or number >= 2**256:
        raise ValueError("invalid %s" % name)
    return str(number)


def _evm_address(value, name: str) -> str:
    if type(value) is not str or not _EVM_ADDRESS_RE.fullmatch(value):
        raise ValueError("invalid %s" % name)
    return value.lower()


def _hex32(value, name: str) -> str:
    if type(value) is not str or not _EVM_BYTES32_RE.fullmatch(value):
        raise ValueError("invalid %s" % name)
    return value.lower()


def _base_authorization(payload: dict) -> dict:
    inner = payload.get("payload") if isinstance(payload, dict) else None
    if not isinstance(inner, dict):
        raise ValueError("missing base payment payload")
    auth = inner.get("authorization")
    permit = inner.get("permit2Authorization")
    if isinstance(auth, dict) and permit is None:
        required = ("from", "to", "value", "validAfter", "validBefore", "nonce")
        if any(key not in auth for key in required):
            raise ValueError("incomplete EIP-3009 authorization")
        return {
            "kind": "eip3009",
            "from": _evm_address(auth.get("from"), "from"),
            "to": _evm_address(auth.get("to"), "to"),
            "value": _uint_text(auth.get("value"), "value"),
            "validAfter": _uint_text(auth.get("validAfter"), "validAfter"),
            "validBefore": _uint_text(auth.get("validBefore"), "validBefore"),
            "nonce": _hex32(auth.get("nonce"), "nonce"),
        }
    if isinstance(permit, dict) and auth is None:
        permitted = permit.get("permitted")
        witness = permit.get("witness")
        if not isinstance(permitted, dict) or not isinstance(witness, dict):
            raise ValueError("incomplete Permit2 authorization")
        return {
            "kind": "permit2",
            "from": _evm_address(permit.get("from"), "from"),
            "token": _evm_address(permitted.get("token"), "token"),
            "amount": _uint_text(permitted.get("amount"), "amount"),
            "spender": _evm_address(permit.get("spender"), "spender"),
            "nonce": _uint_text(permit.get("nonce"), "nonce"),
            "deadline": _uint_text(permit.get("deadline"), "deadline"),
            "to": _evm_address(witness.get("to"), "witness.to"),
            "validAfter": _uint_text(witness.get("validAfter"), "witness.validAfter"),
        }
    raise ValueError("ambiguous base authorization")


def _strict_b64(value, *, maximum: int) -> bytes:
    if type(value) is not str or not value or len(value) > MAX_INNER_B64_TEXT:
        raise ValueError("invalid base64 payment bytes")
    if not value.isascii() or any(ch.isspace() for ch in value):
        raise ValueError("invalid base64 payment bytes")
    if "=" in value[:-2]:
        raise ValueError("invalid base64 padding")
    padded = value + ("=" * ((4 - len(value) % 4) % 4))
    try:
        raw = base64.b64decode(padded, validate=True)
    except Exception as exc:
        raise ValueError("invalid base64 payment bytes") from exc
    if not raw or len(raw) > maximum:
        raise ValueError("invalid payment byte length")
    return raw


def _shortvec(buf: bytes) -> tuple[int, int]:
    """Canonical Solana compact-u16 at the beginning of a transaction."""
    value = 0
    shift = 0
    for index in range(3):
        if index >= len(buf):
            raise ValueError("truncated Solana signature count")
        byte = buf[index]
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            if index and byte == 0:
                raise ValueError("non-canonical Solana signature count")
            if value > 0xFFFF:
                raise ValueError("invalid Solana signature count")
            return value, index + 1
        shift += 7
    raise ValueError("invalid Solana signature count")


def _solana_authorization(payload: dict) -> str:
    inner = payload.get("payload") if isinstance(payload, dict) else None
    if not isinstance(inner, dict):
        raise ValueError("missing Solana payment payload")
    raw = _strict_b64(inner.get("transaction"), maximum=MAX_SOLANA_TRANSACTION_BYTES)
    count, offset = _shortvec(raw)
    if count < 1 or count > 32:
        raise ValueError("invalid Solana signature count")
    message_offset = offset + (count * 64)
    if message_offset >= len(raw):
        raise ValueError("truncated Solana transaction")
    # Signatures are intentionally excluded. The serialized message is the
    # economic authorization and is what every transaction signature covers.
    return hashlib.sha256(raw[message_offset:]).hexdigest()


def _algorand_authorization(payload: dict) -> dict:
    from live402 import algo_tx

    inner = payload.get("payload") if isinstance(payload, dict) else None
    if not isinstance(inner, dict):
        raise ValueError("missing Algorand payment payload")
    group = inner.get("paymentGroup")
    index = inner.get("paymentIndex")
    if type(index) is not int or not isinstance(group, list):
        raise ValueError("invalid Algorand payment group")
    if not group or len(group) > MAX_ALGORAND_GROUP or index < 0 or index >= len(group):
        raise ValueError("invalid Algorand payment group")
    txids: list[str] = []
    for encoded in group:
        raw = _strict_b64(encoded, maximum=algo_tx.MAX_MSGPACK_BYTES)
        obj = algo_tx.msgpack_decode(raw, strict=True)
        if "txn" in obj:
            txn = obj.get("txn")
        else:
            if any(key in obj for key in ("sig", "msig", "lsig", "sgnr", "pqsig")):
                raise ValueError("signed Algorand wrapper missing txn")
            txn = obj
        if not isinstance(txn, dict) or not txn:
            raise ValueError("invalid Algorand transaction")
        # Canonical unsigned transaction ids strip mutable signature wrappers.
        txids.append(algo_tx.txid_from_unsigned(txn))
    return {"paymentIndex": index, "txids": txids}


def canonical_fingerprint(payload: dict, accept: dict) -> str:
    """Hash the rail-specific economic authorization, never its unsigned wrapper."""
    req = payment.official_requirements(accept if isinstance(accept, dict) else {})
    rail = payment.rail_of_accept(accept if isinstance(accept, dict) else {})
    if rail == "base":
        authorization = _base_authorization(payload)
    elif rail == "solana":
        authorization = _solana_authorization(payload)
    elif rail == "algorand":
        authorization = _algorand_authorization(payload)
    else:
        raise ValueError("unsupported replay rail")
    material = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "authorization": authorization,
        "rail": rail,
        "network": req.get("network"),
        "asset": req.get("asset"),
        "amount": str(req.get("amount") or ""),
        "payTo": req.get("payTo"),
        "scheme": req.get("scheme"),
    }
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def legacy_fingerprint(payload: dict, accept: dict) -> str:
    """Exact v1 wrapper fingerprint for read-only compatibility with old rows."""
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
    __slots__ = ("event", "result", "scope_hash")

    def __init__(self, scope_hash: str | None) -> None:
        self.event = threading.Event()
        self.result: tuple | None = None
        self.scope_hash = scope_hash


_lock = threading.Lock()
_inflight: dict[str, _Entry] = {}
_completed: dict[str, tuple[float, str | None, tuple]] = {}
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
                and _identity_cutover_ready(conn)
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
    if "fingerprint_version" not in cols:
        conn.execute(
            "ALTER TABLE settle_ledger ADD COLUMN fingerprint_version INTEGER NOT NULL DEFAULT 1"
        )
        conn.commit()
    if "scope_hash" not in cols:
        conn.execute("ALTER TABLE settle_ledger ADD COLUMN scope_hash TEXT")
        conn.commit()
    legacy = conn.execute(
        "SELECT 1 FROM settle_ledger WHERE fingerprint_version < ? LIMIT 1",
        (FINGERPRINT_VERSION,),
    ).fetchone()
    acknowledged = conn.execute(
        "SELECT value FROM replay_meta WHERE key = ?", (_CUTOVER_META_KEY,)
    ).fetchone()
    if (
        legacy
        and not acknowledged
        and (os.environ.get(CUTOVER_ACK_ENV) or "").strip() == CUTOVER_ACK_VALUE
    ):
        conn.execute(
            "INSERT OR REPLACE INTO replay_meta (key, value) VALUES (?, ?)",
            (_CUTOVER_META_KEY, str(int(time.time()))),
        )
        conn.commit()


def _identity_cutover_ready(conn: sqlite3.Connection) -> bool:
    legacy = conn.execute(
        "SELECT 1 FROM settle_ledger WHERE fingerprint_version < ? LIMIT 1",
        (FINGERPRINT_VERSION,),
    ).fetchone()
    if not legacy:
        return True
    return bool(
        conn.execute(
            "SELECT 1 FROM replay_meta WHERE key = ? LIMIT 1", (_CUTOVER_META_KEY,)
        ).fetchone()
    )


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


def _sanitize_outcome(result: tuple) -> tuple:
    """Redact current and legacy cached results before persistence or replay."""
    code, body, extra = result
    blocked_containers = {
        "facilitator_response",
        "headers",
        "payment_payload",
        "paymentpayload",
        "request_headers",
    }
    blocked_top_level = {
        "authorization",
        "errorreason",
        "invalidreason",
        "signature",
    }

    def cleanse(value, depth=0):
        if depth > 16:
            return None
        if isinstance(value, dict):
            return {
                str(key): cleanse(item, depth + 1)
                for key, item in value.items()
                if str(key).replace("-", "_").lower() not in blocked_containers
                and not (
                    depth == 0
                    and str(key).replace("-", "_").lower() in blocked_top_level
                )
            }
        if isinstance(value, list):
            return [cleanse(item, depth + 1) for item in value[:256]]
        return value

    safe_body = cleanse(body) if isinstance(body, dict) else {}
    if int(code) == 402 and "error" in safe_body:
        safe_body["error"] = "Payment processing failed"
    safe_extra = None
    if isinstance(extra, dict):
        header = extra.get("PAYMENT-RESPONSE")
        billing = safe_body.get("billing")
        receipt_allowed = (
            isinstance(billing, dict)
            and billing.get("settlement_attempted") is True
            and billing.get("settled") is True
        )
        if isinstance(header, str) and receipt_allowed:
            decoded = payment._decode_payment_blob(header)
            rail = billing.get("rail") if isinstance(billing, dict) else None
            receipt = payment.sanitize_settlement_receipt(decoded, rail=rail)
            if receipt:
                safe_extra = {
                    "PAYMENT-RESPONSE": payment.payment_response_header(receipt)
                }
        required = extra.get("PAYMENT-REQUIRED")
        if isinstance(required, str):
            safe_extra = dict(safe_extra or {})
            safe_extra["PAYMENT-REQUIRED"] = required
    return int(code), safe_body, safe_extra


def _encode_outcome(result: tuple) -> str:
    result = _sanitize_outcome(result)
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
    return _sanitize_outcome((code, body, extra))


def _explicit_outcome_state(result: tuple) -> str | None:
    """Read a valid success-only outcome state; legacy rows return None."""
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
    state = billing.get("settlement_state")
    if state == "settled" and attempted is True and settled is True:
        if code in (200, 503):
            return STATE_SETTLED
    if state == "not_attempted" and attempted is False and settled is False:
        if code == 503 and body.get("live") is False:
            return STATE_NOT_SETTLED
    if state == "rejected" and type(attempted) is bool and settled is False:
        if code == 402:
            return STATE_REJECTED
    if state == "unknown" and attempted in (True, None) and settled is None:
        if code == 503:
            return STATE_UNKNOWN
    # Compatibility with outcomes written by the first success-only commit.
    if type(attempted) is bool and type(settled) is bool:
        if settled and attempted and code in (200, 503):
            return STATE_SETTLED
        if not settled and not attempted and code == 503 and body.get("live") is False:
            return STATE_NOT_SETTLED
    return None


def _ledger_lookup(
    fp_hash: str,
    scope_hash: str | None = None,
    *,
    enforce_scope: bool = True,
) -> tuple[str, tuple | None]:
    """Read a durable row. missing / cached / reject. Fail closed on sqlite errors.

    Non-terminal states (settlement_pending, unknown) never replay a
    success and never authorize a second settle.
    """
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT state, outcome_json, fingerprint_version, scope_hash "
            "FROM settle_ledger WHERE fp_hash = ?",
            (fp_hash,),
        ).fetchone()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return "reject", None
    if row is None:
        return "missing", None
    state, outcome, version, stored_scope = row
    try:
        version_number = int(version or 1)
    except (TypeError, ValueError):
        return "reject", None
    if enforce_scope and version_number >= FINGERPRINT_VERSION:
        if stored_scope != scope_hash:
            return "reject", None
    if state == STATE_UNKNOWN and outcome:
        decoded = _decode_outcome(outcome)
        if decoded is not None and _explicit_outcome_state(decoded) == STATE_UNKNOWN:
            return "cached", decoded
    if state in NON_TERMINAL_STATES or state not in TERMINAL_STATES:
        return "reject", None
    if outcome:
        decoded = _decode_outcome(outcome)
        if decoded is not None:
            return "cached", decoded
    return "reject", None


def _ledger_reserve(fp_hash: str, scope_hash: str | None = None) -> str:
    """INSERT settlement_pending. run or reject. UNIQUE is the inter-process lock."""
    try:
        conn = _connect()
        if not _identity_cutover_ready(conn):
            return "reject"
        conn.execute(
            "INSERT INTO settle_ledger "
            "(fp_hash, state, outcome_json, created_at, fingerprint_version, scope_hash) "
            "VALUES (?, ?, NULL, ?, ?, ?)",
            (fp_hash, STATE_PENDING, time.time(), FINGERPRINT_VERSION, scope_hash),
        )
        conn.commit()
        return "run"
    except sqlite3.IntegrityError:
        try:
            conn.rollback()
        except Exception:
            pass
        return "reject"
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return "reject"


def _ledger_finish(fp_hash: str, result: tuple, cache: bool) -> None:
    try:
        conn = _connect()
        if cache:
            explicit = _explicit_outcome_state(result)
            if explicit is not None:
                state = explicit
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
    except (OSError, sqlite3.Error, TypeError, ValueError):
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
    except (OSError, sqlite3.Error, TypeError, ValueError):
        pass


def _prune_completed(now: float) -> None:
    # TTL is the in-memory response cache only. Sqlite uniqueness does not expire.
    stale = [key for key, (exp, _scope, _res) in _completed.items() if exp <= now]
    for key in stale:
        _completed.pop(key, None)
    while len(_completed) > MAX_COMPLETED:
        oldest = next(iter(_completed))
        _completed.pop(oldest, None)


def peek_completed(fp: str, scope: str | None = None) -> tuple | None:
    now = clock.monotonic()
    with _lock:
        _prune_completed(now)
        hit = _completed.get(fp)
        if not hit:
            return None
        exp, stored_scope, result = hit
        if exp <= now:
            _completed.pop(fp, None)
            return None
        if stored_scope != _scope_hash(scope):
            return None
        return result


def _scope_hash(scope: str | None) -> str | None:
    if scope is None:
        return None
    return hashlib.sha256(("replay-scope-v1:" + str(scope)).encode("utf-8")).hexdigest()


def begin(
    fp: str,
    legacy_fp: str | None = None,
    scope: str | None = None,
) -> tuple[str, _Entry | tuple | None]:
    """Acquire execution, return a cached result, wait, or reject a duplicate.

    A second process that hits the UNIQUE row is rejected (fail closed).
    """
    now = clock.monotonic()
    fp_hash = durable_hash(fp)
    scope_hash = _scope_hash(scope)
    with _lock:
        _prune_completed(now)
        cached = _completed.get(fp)
        if cached and cached[0] > now:
            if cached[1] != scope_hash:
                return "reject", None
            return "cached", cached[2]
        existing = _inflight.get(fp)
        if existing is not None:
            if existing.scope_hash != scope_hash:
                return "reject", None
            return "wait", existing
        status, persisted = _ledger_lookup(fp_hash, scope_hash)
        if status == "cached":
            return "cached", persisted
        if status == "reject":
            return "reject", None
        if legacy_fp and legacy_fp != fp:
            legacy_status, legacy_persisted = _ledger_lookup(
                durable_hash(legacy_fp), enforce_scope=False
            )
            if legacy_status == "cached":
                return "cached", legacy_persisted
            if legacy_status == "reject":
                return "reject", None
        if _ledger_reserve(fp_hash, scope_hash) != "run":
            return "reject", None
        entry = _Entry(scope_hash)
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
    safe_result = _sanitize_outcome(result)
    with _lock:
        entry = _inflight.get(fp)
        if entry is not None:
            entry.result = safe_result
            entry.event.set()
            _inflight.pop(fp, None)
        if cache:
            _prune_completed(now)
            scope_hash = entry.scope_hash if entry is not None else None
            _completed[fp] = (now + COMPLETED_TTL_SECONDS, scope_hash, safe_result)
            _prune_completed(now)
        _ledger_finish(fp_hash, safe_result, cache)


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
