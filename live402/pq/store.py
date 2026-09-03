"""SQLite transparency log. Separate file from catalog.sqlite and history.sqlite.

leaves(idx PK, body BLOB, leaf_hash BLOB)
compact tree hashes
tiles(level, n, width, data)
entry_bundles (uint16 BE length + bytes)
meta origin/vkey/size/checkpoint
optional checkpoints(size PK, note TEXT)

Anchor state is split:
  last_authorized_checkpoint — authenticated signer response persisted (not on-chain)
  last_confirmed_checkpoint  — persisted TestNet inclusion fields (txid/round/root)

Migration: new tables authorized_anchors / confirmed_anchors and meta keys
last_authorized_checkpoint / last_confirmed_checkpoint. Confirmed defaults
empty. Legacy meta['anchor'] {size, at} is authorized-only, never confirmed.

Publish order: entry bundles → tiles → tree → signed checkpoint.
Never checkpoint a size whose tiles/bundles are missing.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable

from live402.pq import ORIGIN
from live402.pq import log_identity
from live402.pq import merkle
from live402.pq import tiles as tilemod

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None


class ConflictError(ValueError):
    """Refusing a confirmed-checkpoint write that would fork history."""


class StoreError(RuntimeError):
    """Fail closed: state regression or immutable canary field mutation."""


# AUTHORIZED → SEND_ATTEMPTED → SUBMITTED → CONFIRMED only forward.
# SEND_INTENT ranks with AUTHORIZED (pre-latch). Empty is AUTHORIZED.
_SEND_STATE_RANK = {
    "": 0,
    "AUTHORIZED": 0,
    "SEND_INTENT": 0,
    "SEND_ATTEMPTED": 1,
    "SUBMITTED": 2,
    "CONFIRMED": 3,
}
_LATCHED_RANK = 1  # SEND_ATTEMPTED and later: blob/txid/policy/fv/lv immutable.


# Tests may install a hook between durable append and later steps.
_after_durable_hooks: list[Callable[[int, bytes], None]] = []

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leaves (
    idx INTEGER PRIMARY KEY,
    body BLOB NOT NULL,
    leaf_hash BLOB NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS leaves_leaf_hash ON leaves(leaf_hash);
CREATE TABLE IF NOT EXISTS tree_hashes (
    start_idx INTEGER NOT NULL,
    end_idx INTEGER NOT NULL,
    hash BLOB NOT NULL,
    PRIMARY KEY (start_idx, end_idx)
);
CREATE TABLE IF NOT EXISTS tiles (
    level INTEGER NOT NULL,
    n INTEGER NOT NULL,
    width INTEGER NOT NULL,
    data BLOB NOT NULL,
    PRIMARY KEY (level, n, width)
);
CREATE TABLE IF NOT EXISTS entry_bundles (
    n INTEGER NOT NULL,
    width INTEGER NOT NULL,
    data BLOB NOT NULL,
    PRIMARY KEY (n, width)
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    size INTEGER PRIMARY KEY,
    note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS authorized_anchors (
    tree_size INTEGER PRIMARY KEY,
    origin TEXT NOT NULL,
    root TEXT NOT NULL,
    checkpoint TEXT NOT NULL,
    request_id TEXT NOT NULL,
    signed BLOB NOT NULL,
    at INTEGER NOT NULL,
    submitted INTEGER NOT NULL DEFAULT 0,
    txid TEXT NOT NULL DEFAULT '',
    send_state TEXT NOT NULL DEFAULT '',
    expected_txid TEXT NOT NULL DEFAULT '',
    fee_policy TEXT NOT NULL DEFAULT '',
    fv INTEGER NOT NULL DEFAULT 0,
    lv INTEGER NOT NULL DEFAULT 0,
    send_attempted_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS confirmed_anchors (
    tree_size INTEGER PRIMARY KEY,
    origin TEXT NOT NULL,
    root TEXT NOT NULL,
    txid TEXT NOT NULL,
    confirmed_round INTEGER NOT NULL,
    at INTEGER NOT NULL,
    network TEXT NOT NULL DEFAULT '',
    genesis_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS anchor_automation_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    confirmed_size INTEGER NOT NULL DEFAULT 0,
    observed_tree_size INTEGER NOT NULL DEFAULT 0,
    unanchored_since INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS anchor_automation_jobs (
    tree_size INTEGER PRIMARY KEY,
    origin TEXT NOT NULL,
    root TEXT NOT NULL,
    checkpoint TEXT NOT NULL,
    request_id TEXT NOT NULL,
    params TEXT NOT NULL,
    authorize_at INTEGER NOT NULL,
    status TEXT NOT NULL,
    resign_count INTEGER NOT NULL DEFAULT 0,
    sign_attempts INTEGER NOT NULL DEFAULT 0,
    superseded_signed_sha256 TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS anchor_automation_sends (
    tree_size INTEGER PRIMARY KEY,
    expected_txid TEXT NOT NULL UNIQUE,
    fee INTEGER NOT NULL,
    attempted_at INTEGER NOT NULL,
    status TEXT NOT NULL
);
"""


def db_path() -> str:
    return log_identity.resolve_db_path()


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
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass
    conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript(_SCHEMA)
    conn.commit()
    _conn = conn
    _conn_path = path
    _chmod_db_files(path)
    _ensure_meta(conn)
    _migrate_authorized_confirmed(conn)
    _migrate_canary_state(conn)
    _migrate_authorized_submit(conn)
    _migrate_confirmed_network(conn)
    try:
        have = _size_unlocked(conn)
        if have and not _ready_unlocked(conn, have):
            _publish_unlocked(conn, have)
    except Exception:
        pass
    return conn


def _ensure_meta(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("SELECT v FROM meta WHERE k = 'origin'")
    row = cur.fetchone()
    if not row:
        conn.execute("INSERT INTO meta(k, v) VALUES ('origin', ?)", (log_identity.configured_origin(),))
        conn.execute("INSERT INTO meta(k, v) VALUES ('size', '0')")
        conn.execute("INSERT INTO meta(k, v) VALUES ('vkey', '')")
        conn.execute("INSERT INTO meta(k, v) VALUES ('checkpoint', '')")
        conn.execute("INSERT INTO meta(k, v) VALUES ('last_authorized_checkpoint', '')")
        conn.execute("INSERT INTO meta(k, v) VALUES ('last_confirmed_checkpoint', '')")
        conn.commit()


def _migrate_authorized_confirmed(conn: sqlite3.Connection) -> None:
    """Legacy meta['anchor'] is authorized-only. Confirmed stays empty unless set."""
    cur = conn.execute("SELECT v FROM meta WHERE k = 'last_authorized_checkpoint'")
    have_auth = cur.fetchone()
    if have_auth is None:
        conn.execute("INSERT INTO meta(k, v) VALUES ('last_authorized_checkpoint', '')")
        have_auth = ("",)
    cur = conn.execute("SELECT v FROM meta WHERE k = 'last_confirmed_checkpoint'")
    have_conf = cur.fetchone()
    if have_conf is None:
        conn.execute("INSERT INTO meta(k, v) VALUES ('last_confirmed_checkpoint', '')")
    old = conn.execute("SELECT v FROM meta WHERE k = 'anchor'").fetchone()
    if old and old[0] and not (have_auth and have_auth[0]):
        try:
            data = json.loads(old[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            payload = {
                "size": int(data.get("size") or 0),
                "at": int(data.get("at") or 0),
                "request_id": "",
                "origin": "",
                "root": "",
            }
            conn.execute(
                "INSERT INTO meta(k, v) VALUES ('last_authorized_checkpoint', ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (json.dumps(payload),),
            )
    conn.commit()


def _migrate_authorized_submit(conn: sqlite3.Connection) -> None:
    """Keep submitted + txid on authorized_anchors. Recover last_authorized from rows.

    Columns already exist from the TestNet submit work. Existing signed rows
    are not deleted. If last_authorized meta is empty or its size has no
    signed row, point meta at the latest signed authorized_anchors row.
    """
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(authorized_anchors)")}
    if "submitted" not in cols:
        conn.execute(
            "ALTER TABLE authorized_anchors ADD COLUMN submitted INTEGER NOT NULL DEFAULT 0"
        )
    if "txid" not in cols:
        conn.execute(
            "ALTER TABLE authorized_anchors ADD COLUMN txid TEXT NOT NULL DEFAULT ''"
        )
    raw = conn.execute(
        "SELECT v FROM meta WHERE k = 'last_authorized_checkpoint'"
    ).fetchone()
    meta_size = 0
    if raw and raw[0]:
        try:
            parsed = json.loads(raw[0])
            if isinstance(parsed, dict):
                meta_size = int(parsed.get("size") or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            meta_size = 0
    have = _authorized_row(conn, meta_size) if meta_size else None
    if not have or not have.get("signed"):
        latest = _latest_authorized_row(conn)
        if latest and latest.get("signed"):
            payload = {
                "size": latest["tree_size"],
                "at": latest["at"],
                "request_id": latest["request_id"],
                "origin": latest["origin"],
                "root": latest["root"],
            }
            conn.execute(
                "INSERT INTO meta(k, v) VALUES ('last_authorized_checkpoint', ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (json.dumps(payload),),
            )
    conn.commit()


def _migrate_canary_state(conn: sqlite3.Connection) -> None:
    """Durable canary columns on authorized_anchors. AUTHORIZED rows stay."""
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(authorized_anchors)")}
    additions = (
        ("send_state", "TEXT NOT NULL DEFAULT ''"),
        ("expected_txid", "TEXT NOT NULL DEFAULT ''"),
        ("fee_policy", "TEXT NOT NULL DEFAULT ''"),
        ("fv", "INTEGER NOT NULL DEFAULT 0"),
        ("lv", "INTEGER NOT NULL DEFAULT 0"),
        ("send_attempted_at", "INTEGER NOT NULL DEFAULT 0"),
    )
    for name, spec in additions:
        if name not in cols:
            conn.execute("ALTER TABLE authorized_anchors ADD COLUMN %s %s" % (name, spec))
    conn.execute(
        "UPDATE authorized_anchors SET send_state = 'SUBMITTED' "
        "WHERE submitted = 1 AND txid != '' AND (send_state IS NULL OR send_state = '')"
    )
    conn.execute(
        "UPDATE authorized_anchors SET send_state = 'AUTHORIZED' "
        "WHERE length(signed) > 0 AND (send_state IS NULL OR send_state = '')"
    )
    conn.commit()


def _normalize_confirmed_network(network, genesis_id, origin: str) -> tuple[str, str]:
    """Persist independently known network only. Never read env or secrets.

    TestNet may be recovered from the historical TestNet origin.
    MainNet is stored only when the caller supplies network or genesis_id.
    """
    from live402.pq import algo_anchor
    from live402.pq import network as netcfg

    net = str(network or "").strip().lower()
    gen = str(genesis_id or "").strip()
    if net and net not in {algo_anchor.TESTNET_NAME, algo_anchor.MAINNET_NAME}:
        raise ConflictError("invalid confirmed network")
    if gen:
        mapped = netcfg.network_for_genesis_id(gen)
        if mapped is None:
            raise ConflictError("invalid confirmed genesis")
        if net and net != mapped.name:
            raise ConflictError("network genesis mismatch")
        net = net or mapped.name
    if not net and (origin or "") == ORIGIN:
        net = algo_anchor.TESTNET_NAME
        gen = gen or algo_anchor.TESTNET_GENESIS_ID
    if net == algo_anchor.TESTNET_NAME and not gen:
        gen = algo_anchor.TESTNET_GENESIS_ID
    if net == algo_anchor.MAINNET_NAME and not gen:
        gen = algo_anchor.MAINNET_GENESIS_ID
    return net, gen


def _migrate_confirmed_network(conn: sqlite3.Connection) -> None:
    """Record independently confirmed network/genesis on confirmed_anchors."""
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(confirmed_anchors)")}
    if "network" not in cols:
        conn.execute("ALTER TABLE confirmed_anchors ADD COLUMN network TEXT NOT NULL DEFAULT ''")
    if "genesis_id" not in cols:
        conn.execute("ALTER TABLE confirmed_anchors ADD COLUMN genesis_id TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "UPDATE confirmed_anchors SET network = 'testnet', genesis_id = 'testnet-v1.0' "
        "WHERE (network IS NULL OR network = '') AND origin = ?",
        (ORIGIN,),
    )
    raw = conn.execute("SELECT v FROM meta WHERE k = 'last_confirmed_checkpoint'").fetchone()
    if raw and raw[0]:
        try:
            data = json.loads(raw[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict) and not str(data.get("network") or "").strip():
            try:
                net, gen = _normalize_confirmed_network(
                    data.get("network"),
                    data.get("genesis_id"),
                    str(data.get("origin") or ""),
                )
            except ConflictError:
                net, gen = "", ""
            if net:
                data["network"] = net
                data["genesis_id"] = gen
                conn.execute(
                    "INSERT INTO meta(k, v) VALUES ('last_confirmed_checkpoint', ?) "
                    "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                    (json.dumps(data),),
                )
    conn.commit()


def close() -> None:
    """Close the process-local connection without deleting the file."""
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
            _conn_path = None


def reset() -> None:
    """Delete the PQ log DB (tests)."""
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


def install_after_durable_hook(fn: Callable[[int, bytes], None] | None) -> None:
    """Test hook: called after the leaf is committed, before publish/sign."""
    _after_durable_hooks.clear()
    if fn is not None:
        _after_durable_hooks.append(fn)


def meta_get(key: str) -> str:
    with _lock:
        conn = _connect()
        cur = conn.execute("SELECT v FROM meta WHERE k = ?", (key,))
        row = cur.fetchone()
        return str(row[0]) if row else ""


def meta_set(key: str, value: str) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO meta(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, value),
        )
        conn.commit()
        _chmod_db_files(_conn_path or db_path())


def origin() -> str:
    stored = meta_get("origin")
    if stored:
        return stored
    return log_identity.configured_origin()


def size() -> int:
    with _lock:
        return _size_unlocked(_connect())


def _size_unlocked(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM leaves")
    return int(cur.fetchone()[0] or 0)


def root(tree_size: int | None = None) -> bytes:
    with _lock:
        conn = _connect()
        target = _size_unlocked(conn) if tree_size is None else int(tree_size)
        if target < 0:
            raise ValueError("negative tree size")
        if target == 0:
            return merkle.empty_tree_hash()
        cached = _cached_range(conn, 0, target)
        if cached is not None:
            return cached
        return merkle.mth_range(
            0,
            target,
            lambda a, b: _cached_range(conn, a, b),
            lambda a, b, h: _store_range(conn, a, b, h),
            lambda i: _leaf_hash_at_unlocked(conn, i),
        )


def _cached_range(conn: sqlite3.Connection, start: int, end: int) -> bytes | None:
    cur = conn.execute(
        "SELECT hash FROM tree_hashes WHERE start_idx = ? AND end_idx = ?",
        (start, end),
    )
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def _store_range(conn: sqlite3.Connection, start: int, end: int, digest: bytes) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tree_hashes(start_idx, end_idx, hash) VALUES (?, ?, ?)",
        (start, end, digest),
    )


def _leaf_hash_at_unlocked(conn: sqlite3.Connection, idx: int) -> bytes:
    cur = conn.execute("SELECT leaf_hash FROM leaves WHERE idx = ?", (int(idx),))
    row = cur.fetchone()
    if not row:
        raise ValueError("missing leaf hash")
    return bytes(row[0])


def _leaf_hashes_unlocked(conn: sqlite3.Connection, tree_size: int | None = None) -> list[bytes]:
    if tree_size is None:
        cur = conn.execute("SELECT leaf_hash FROM leaves ORDER BY idx")
    else:
        cur = conn.execute(
            "SELECT leaf_hash FROM leaves WHERE idx < ? ORDER BY idx",
            (int(tree_size),),
        )
    return [bytes(r[0]) for r in cur.fetchall()]


def _bodies_unlocked(conn: sqlite3.Connection, start: int, end: int) -> list[bytes]:
    cur = conn.execute(
        "SELECT body FROM leaves WHERE idx >= ? AND idx < ? ORDER BY idx",
        (start, end),
    )
    return [bytes(r[0]) for r in cur.fetchall()]


def leaf_at(idx: int) -> dict | None:
    with _lock:
        conn = _connect()
        cur = conn.execute("SELECT idx, body, leaf_hash FROM leaves WHERE idx = ?", (int(idx),))
        row = cur.fetchone()
        if not row:
            return None
        return {"idx": int(row[0]), "body": bytes(row[1]), "leaf_hash": bytes(row[2])}


def find_by_hash(leaf_h: bytes) -> dict | None:
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "SELECT idx, body, leaf_hash FROM leaves WHERE leaf_hash = ?",
            (bytes(leaf_h),),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"idx": int(row[0]), "body": bytes(row[1]), "leaf_hash": bytes(row[2])}


def append(body: bytes) -> dict:
    """Durable append. Duplicate body is idempotent (same idx).

    Returns {idx, leaf_hash, size, duplicate}. Does not sign a checkpoint.
    """
    raw = bytes(body)
    digest = merkle.leaf_hash(raw)
    with _lock:
        conn = _connect()
        existing = conn.execute(
            "SELECT idx FROM leaves WHERE leaf_hash = ?",
            (digest,),
        ).fetchone()
        if existing:
            idx = int(existing[0])
            return {
                "idx": idx,
                "leaf_hash": digest,
                "size": _size_unlocked(conn),
                "duplicate": True,
            }
        idx = _size_unlocked(conn)
        conn.execute(
            "INSERT INTO leaves(idx, body, leaf_hash) VALUES (?, ?, ?)",
            (idx, raw, digest),
        )
        new_size = idx + 1
        merkle.incremental_root(
            new_size,
            digest,
            lambda a, b: _cached_range(conn, a, b),
            lambda a, b, h: _store_range(conn, a, b, h),
        )
        conn.execute(
            "INSERT INTO meta(k, v) VALUES ('size', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (str(new_size),),
        )
        conn.commit()
        _chmod_db_files(_conn_path or db_path())
        for hook in list(_after_durable_hooks):
            hook(idx, raw)
        publish_up_to(new_size)
        return {
            "idx": idx,
            "leaf_hash": digest,
            "size": new_size,
            "duplicate": False,
        }


def _publish_unlocked(conn: sqlite3.Connection, tree_size: int) -> None:
    have = _size_unlocked(conn)
    if tree_size < 0 or tree_size > have:
        raise ValueError("publish size out of range")
    hashes = _leaf_hashes_unlocked(conn, tree_size)
    for n, width in tilemod.bundles_required(tree_size):
        start = n * tilemod.TILE_WIDTH
        bodies = _bodies_unlocked(conn, start, start + width)
        data = tilemod.encode_entry_bundle(bodies)
        conn.execute(
            "INSERT OR REPLACE INTO entry_bundles(n, width, data) VALUES (?, ?, ?)",
            (n, width, data),
        )
    for level, n, width in tilemod.tiles_required(tree_size):
        th = tilemod.tile_hashes_for_level(hashes, level, n, width)
        data = tilemod.encode_hash_tile(th)
        conn.execute(
            "INSERT OR REPLACE INTO tiles(level, n, width, data) VALUES (?, ?, ?, ?)",
            (level, n, width, data),
        )
    conn.commit()
    _chmod_db_files(_conn_path or db_path())


def publish_up_to(tree_size: int) -> None:
    """Materialize entry bundles then tiles for `tree_size`. Tree hashes already stored."""
    with _lock:
        _publish_unlocked(_connect(), tree_size)


def _ready_unlocked(conn: sqlite3.Connection, target: int) -> bool:
    have = _size_unlocked(conn)
    if target < 0 or target > have:
        return False
    if target == 0:
        return True
    for n, width in tilemod.bundles_required(target):
        row = conn.execute(
            "SELECT 1 FROM entry_bundles WHERE n = ? AND width = ?",
            (n, width),
        ).fetchone()
        if not row:
            return False
    for level, n, width in tilemod.tiles_required(target):
        row = conn.execute(
            "SELECT 1 FROM tiles WHERE level = ? AND n = ? AND width = ?",
            (level, n, width),
        ).fetchone()
        if not row:
            return False
    return True


def ready_to_checkpoint(tree_size: int | None = None) -> bool:
    """True only when every tile and entry bundle for `tree_size` is present."""
    with _lock:
        conn = _connect()
        have = _size_unlocked(conn)
        target = have if tree_size is None else int(tree_size)
        return _ready_unlocked(conn, target)


def get_tile(level: int, n: int, width: int | None = None) -> bytes | None:
    try:
        safe_n = tilemod.check_tile_index(int(n))
        safe_w = tilemod.TILE_WIDTH if width is None else int(width)
        if safe_w < 1 or safe_w > tilemod.TILE_WIDTH:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "SELECT data FROM tiles WHERE level = ? AND n = ? AND width = ?",
            (level, safe_n, safe_w),
        )
        row = cur.fetchone()
        return bytes(row[0]) if row else None


def get_entry_bundle(n: int, width: int | None = None) -> bytes | None:
    try:
        safe_n = tilemod.check_tile_index(int(n))
        safe_w = tilemod.TILE_WIDTH if width is None else int(width)
        if safe_w < 1 or safe_w > tilemod.TILE_WIDTH:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "SELECT data FROM entry_bundles WHERE n = ? AND width = ?",
            (safe_n, safe_w),
        )
        row = cur.fetchone()
        return bytes(row[0]) if row else None


def inclusion_path(index: int, tree_size: int | None = None) -> list[bytes]:
    with _lock:
        conn = _connect()
        have = _size_unlocked(conn)
        target = have if tree_size is None else int(tree_size)
        return merkle.inclusion_path_cached(
            index,
            target,
            lambda a, b: _cached_range(conn, a, b),
            lambda i: _leaf_hash_at_unlocked(conn, i),
            lambda a, b, h: _store_range(conn, a, b, h),
        )


def consistency_path(old_size: int, new_size: int | None = None) -> list[bytes]:
    with _lock:
        conn = _connect()
        have = _size_unlocked(conn)
        target = have if new_size is None else int(new_size)
        return merkle.consistency_path_cached(
            old_size,
            target,
            lambda a, b: _cached_range(conn, a, b),
            lambda i: _leaf_hash_at_unlocked(conn, i),
            lambda a, b, h: _store_range(conn, a, b, h),
        )


def save_checkpoint(tree_size: int, note: str) -> None:
    if not ready_to_checkpoint(tree_size):
        raise ValueError("refusing to checkpoint a size with missing tiles/bundles")
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT OR REPLACE INTO checkpoints(size, note) VALUES (?, ?)",
            (int(tree_size), note),
        )
        conn.execute(
            "INSERT INTO meta(k, v) VALUES ('checkpoint', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (note,),
        )
        conn.commit()
        _chmod_db_files(_conn_path or db_path())


def latest_checkpoint() -> str:
    return meta_get("checkpoint")


def checkpoint_at(tree_size: int) -> str:
    with _lock:
        conn = _connect()
        cur = conn.execute("SELECT note FROM checkpoints WHERE size = ?", (int(tree_size),))
        row = cur.fetchone()
        return str(row[0]) if row else ""


def _authorized_row(conn: sqlite3.Connection, tree_size: int) -> dict | None:
    cur = conn.execute(
        "SELECT tree_size, origin, root, checkpoint, request_id, signed, at, "
        "submitted, txid, send_state, expected_txid, fee_policy, fv, lv, "
        "send_attempted_at FROM authorized_anchors WHERE tree_size = ?",
        (int(tree_size),),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "tree_size": int(row[0]),
        "size": int(row[0]),
        "origin": str(row[1] or ""),
        "root": str(row[2] or ""),
        "checkpoint": str(row[3] or ""),
        "request_id": str(row[4] or ""),
        "signed": bytes(row[5]) if row[5] is not None else b"",
        "at": int(row[6] or 0),
        "submitted": bool(int(row[7] or 0)),
        "txid": str(row[8] or ""),
        "send_state": str(row[9] or ""),
        "expected_txid": str(row[10] or ""),
        "fee_policy": str(row[11] or ""),
        "fv": int(row[12] or 0),
        "lv": int(row[13] or 0),
        "send_attempted_at": int(row[14] or 0),
    }


def authorized_at(tree_size: int) -> dict | None:
    with _lock:
        return _authorized_row(_connect(), int(tree_size))


def _latest_authorized_row(conn: sqlite3.Connection) -> dict | None:
    cur = conn.execute(
        "SELECT tree_size FROM authorized_anchors "
        "WHERE signed IS NOT NULL AND length(signed) > 0 "
        "ORDER BY tree_size DESC LIMIT 1"
    )
    row = cur.fetchone()
    if not row:
        return None
    return _authorized_row(conn, int(row[0]))


def last_authorized_checkpoint() -> dict:
    raw = meta_get("last_authorized_checkpoint")
    data = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
    size = int(data.get("size") or 0)
    out = {
        "size": size,
        "at": int(data.get("at") or 0),
        "request_id": str(data.get("request_id") or ""),
        "origin": str(data.get("origin") or ""),
        "root": str(data.get("root") or ""),
        "submitted": False,
        "txid": "",
        "send_state": "",
        "expected_txid": "",
        "fee_policy": "",
        "fv": 0,
        "lv": 0,
        "send_attempted_at": 0,
    }
    row = authorized_at(size) if size else None
    if (not row or not row.get("signed")):
        with _lock:
            row = _latest_authorized_row(_connect())
        if row:
            size = int(row.get("tree_size") or row.get("size") or 0)
            out["size"] = size
    if row:
        out.update(
            {
                "origin": row["origin"] or out["origin"],
                "root": row["root"] or out["root"],
                "request_id": row["request_id"] or out["request_id"],
                "checkpoint": row["checkpoint"],
                "signed": row["signed"],
                "at": row["at"] or out["at"],
                "tree_size": row["tree_size"],
                "submitted": bool(row.get("submitted")),
                "txid": str(row.get("txid") or ""),
                "send_state": str(row.get("send_state") or ""),
                "expected_txid": str(row.get("expected_txid") or ""),
                "fee_policy": str(row.get("fee_policy") or ""),
                "fv": int(row.get("fv") or 0),
                "lv": int(row.get("lv") or 0),
                "send_attempted_at": int(row.get("send_attempted_at") or 0),
            }
        )
    return out


def send_state_rank(state: str | None) -> int:
    """Numeric rank. Unknown states fail closed."""
    key = str(state or "").strip()
    if key not in _SEND_STATE_RANK:
        raise StoreError("unknown send_state")
    return _SEND_STATE_RANK[key]


def _policy_text(fee_policy) -> str:
    if fee_policy is None:
        return ""
    if isinstance(fee_policy, str):
        return fee_policy
    return json.dumps(fee_policy)


def save_authorized_checkpoint(
    *,
    tree_size: int,
    origin: str,
    root,
    checkpoint: str,
    request_id: str,
    signed: bytes,
    at: int,
    submitted: bool = False,
    txid: str = "",
    send_state: str = "",
    expected_txid: str = "",
    fee_policy: str = "",
    fv: int = 0,
    lv: int = 0,
    send_attempted_at: int = 0,
) -> dict:
    """Persist AUTHORIZED only. Same checkpoint is idempotent. Different checkpoint refused.

    submitted + txid record a POST attempt. POST success is not confirmation.
    send_state advances AUTHORIZED -> SEND_ATTEMPTED -> SUBMITTED -> CONFIRMED
    only forward. After SEND_ATTEMPTED the signed blob, expected_txid,
    origin, tree_size, root, checkpoint, fee_policy, fv, and lv are
    immutable. Mutation fail-closes. Tests that need a pre-latched row
    must use the dedicated fixture helper, not this function.
    """
    size = int(tree_size)
    root_hex = bytes(root).hex() if isinstance(root, (bytes, bytearray)) else str(root or "")
    blob = bytes(signed or b"")
    note = checkpoint if checkpoint is not None else ""
    rid = request_id if request_id is not None else ""
    when = int(at)
    want_submitted = bool(submitted)
    want_txid = str(txid or "").strip()
    want_state = str(send_state or "").strip()
    want_expected = str(expected_txid or "").strip()
    want_policy = _policy_text(fee_policy)
    want_fv = int(fv or 0)
    want_lv = int(lv or 0)
    want_attempted = int(send_attempted_at or 0)
    if want_state:
        send_state_rank(want_state)
    with _lock:
        conn = _connect()
        existing = _authorized_row(conn, size)
        if existing and existing["signed"]:
            have_state = str(existing.get("send_state") or "").strip()
            have_rank = send_state_rank(have_state)
            if want_state:
                want_rank = send_state_rank(want_state)
                if want_rank < have_rank:
                    raise StoreError("send_state regression")
            if have_rank >= _LATCHED_RANK:
                if blob and existing["signed"] and blob != existing["signed"]:
                    raise StoreError("immutable signed blob")
                if want_expected and existing.get("expected_txid") and want_expected != str(existing.get("expected_txid") or ""):
                    raise StoreError("immutable expected_txid")
                if origin and existing.get("origin") and str(origin) != str(existing.get("origin") or ""):
                    raise StoreError("immutable origin")
                if root_hex and existing.get("root") and root_hex != str(existing.get("root") or ""):
                    raise StoreError("immutable root")
                if note and existing.get("checkpoint") and note != str(existing.get("checkpoint") or ""):
                    raise StoreError("immutable checkpoint")
                if want_policy and existing.get("fee_policy") and want_policy != str(existing.get("fee_policy") or ""):
                    raise StoreError("immutable fee_policy")
                if want_fv and existing.get("fv") and want_fv != int(existing.get("fv") or 0):
                    raise StoreError("immutable fv")
                if want_lv and existing.get("lv") and want_lv != int(existing.get("lv") or 0):
                    raise StoreError("immutable lv")
            else:
                if existing["checkpoint"] != note or (existing["root"] and root_hex and existing["root"] != root_hex):
                    return existing
                if existing["signed"] and blob and existing["signed"] != blob:
                    return existing
            sets = []
            args: list = []
            if want_submitted and want_txid:
                sets.append("submitted = 1")
                sets.append("txid = ?")
                args.append(want_txid)
            if want_state:
                sets.append("send_state = ?")
                args.append(want_state)
            if want_expected:
                sets.append("expected_txid = ?")
                args.append(want_expected)
            if want_policy and have_rank < _LATCHED_RANK:
                sets.append("fee_policy = ?")
                args.append(want_policy)
            if want_fv and have_rank < _LATCHED_RANK:
                sets.append("fv = ?")
                args.append(want_fv)
            if want_lv and have_rank < _LATCHED_RANK:
                sets.append("lv = ?")
                args.append(want_lv)
            if want_attempted:
                sets.append("send_attempted_at = ?")
                args.append(want_attempted)
            if sets:
                args.append(size)
                conn.execute(
                    "UPDATE authorized_anchors SET " + ", ".join(sets) + " WHERE tree_size = ?",
                    args,
                )
            payload = {
                "size": existing["tree_size"],
                "at": existing["at"],
                "request_id": existing["request_id"],
                "origin": existing["origin"],
                "root": existing["root"],
            }
            conn.execute(
                "INSERT INTO meta(k, v) VALUES ('last_authorized_checkpoint', ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (json.dumps(payload),),
            )
            conn.commit()
            return _authorized_row(conn, size) or existing
        conn.execute(
            "INSERT OR REPLACE INTO authorized_anchors"
            "(tree_size, origin, root, checkpoint, request_id, signed, at, submitted, txid, "
            "send_state, expected_txid, fee_policy, fv, lv, send_attempted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                size,
                origin or "",
                root_hex,
                note,
                rid,
                blob,
                when,
                1 if want_submitted else 0,
                want_txid,
                want_state or "AUTHORIZED",
                want_expected,
                want_policy or "",
                want_fv,
                want_lv,
                want_attempted,
            ),
        )
        payload = {
            "size": size,
            "at": when,
            "request_id": rid,
            "origin": origin or "",
            "root": root_hex,
        }
        conn.execute(
            "INSERT INTO meta(k, v) VALUES ('last_authorized_checkpoint', ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (json.dumps(payload),),
        )
        conn.commit()
        _chmod_db_files(_conn_path or db_path())
        return _authorized_row(conn, size) or {
            "tree_size": size,
            "size": size,
            "origin": origin or "",
            "root": root_hex,
            "checkpoint": note,
            "request_id": rid,
            "signed": blob,
            "at": when,
            "submitted": want_submitted,
            "txid": want_txid,
            "send_state": want_state or "AUTHORIZED",
            "expected_txid": want_expected,
            "fee_policy": want_policy or "",
            "fv": want_fv,
            "lv": want_lv,
            "send_attempted_at": want_attempted,
        }


def discard_authorized_checkpoint(tree_size: int) -> None:
    """Explicit operator discard of AUTHORIZED only. Never after SEND_ATTEMPTED."""
    size = int(tree_size)
    with _lock:
        conn = _connect()
        existing = _authorized_row(conn, size)
        if not existing:
            return
        if send_state_rank(existing.get("send_state")) >= _LATCHED_RANK:
            raise StoreError("cannot discard after SEND_ATTEMPTED")
        conn.execute("DELETE FROM authorized_anchors WHERE tree_size = ?", (size,))
        raw = meta_get("last_authorized_checkpoint")
        try:
            data = json.loads(raw) if raw else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict) and int(data.get("size") or 0) == size:
            conn.execute(
                "INSERT INTO meta(k, v) VALUES ('last_authorized_checkpoint', ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (json.dumps({"size": 0, "at": 0, "request_id": "", "origin": "", "root": ""}),),
            )
        conn.commit()


_AUTO_STATUS_RANK = {
    "PENDING": 0,
    "AUTHORIZED": 1,
    "SEND_ATTEMPTED": 2,
    "SUBMITTED": 3,
    "CONFIRMED": 4,
    "HALTED": 5,
}


def automation_observe(*, tree_size: int, confirmed_size: int, now: int) -> dict:
    """Durably track when the first currently-unanchored leaf was observed."""
    current = max(0, int(tree_size))
    confirmed = max(0, int(confirmed_size))
    when = int(now)
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT confirmed_size, observed_tree_size, unanchored_since, updated_at "
            "FROM anchor_automation_state WHERE singleton = 1"
        ).fetchone()
        prior_confirmed = int(row[0] or 0) if row else 0
        prior_current = int(row[1] or 0) if row else 0
        since = int(row[2] or 0) if row else 0
        if current <= confirmed:
            since = 0
        elif not row or confirmed != prior_confirmed or since < 1:
            since = when
        if (
            row
            and confirmed == prior_confirmed
            and current == prior_current
            and since == int(row[2] or 0)
        ):
            # The worker wakes frequently. Avoid a SQLite commit when the
            # observation did not change; this keeps WAL/volume churn tied
            # to actual log growth or confirmation progress.
            return {
                "confirmed_size": prior_confirmed,
                "observed_tree_size": prior_current,
                "unanchored_since": since,
                "updated_at": int(row[3] or 0),
            }
        conn.execute(
            "INSERT INTO anchor_automation_state"
            "(singleton, confirmed_size, observed_tree_size, unanchored_since, updated_at) "
            "VALUES (1, ?, ?, ?, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET "
            "confirmed_size = excluded.confirmed_size, "
            "observed_tree_size = excluded.observed_tree_size, "
            "unanchored_since = excluded.unanchored_since, "
            "updated_at = excluded.updated_at",
            (confirmed, current, since, when),
        )
        conn.commit()
        return {
            "confirmed_size": confirmed,
            "observed_tree_size": current,
            "unanchored_since": since,
            "updated_at": when,
        }


def automation_state() -> dict:
    with _lock:
        row = _connect().execute(
            "SELECT confirmed_size, observed_tree_size, unanchored_since, updated_at "
            "FROM anchor_automation_state WHERE singleton = 1"
        ).fetchone()
        if not row:
            return {
                "confirmed_size": 0,
                "observed_tree_size": 0,
                "unanchored_since": 0,
                "updated_at": 0,
            }
        return {
            "confirmed_size": int(row[0] or 0),
            "observed_tree_size": int(row[1] or 0),
            "unanchored_since": int(row[2] or 0),
            "updated_at": int(row[3] or 0),
        }


def _automation_job_row(conn: sqlite3.Connection, tree_size: int) -> dict | None:
    row = conn.execute(
        "SELECT tree_size, origin, root, checkpoint, request_id, params, authorize_at, "
        "status, resign_count, sign_attempts, superseded_signed_sha256, "
        "last_error, updated_at "
        "FROM anchor_automation_jobs WHERE tree_size = ?",
        (int(tree_size),),
    ).fetchone()
    if not row:
        return None
    try:
        params = json.loads(str(row[5] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        params = {}
    return {
        "tree_size": int(row[0]),
        "origin": str(row[1] or ""),
        "root": str(row[2] or ""),
        "checkpoint": str(row[3] or ""),
        "request_id": str(row[4] or ""),
        "params": params if isinstance(params, dict) else {},
        "authorize_at": int(row[6] or 0),
        "status": str(row[7] or ""),
        "resign_count": int(row[8] or 0),
        "sign_attempts": int(row[9] or 0),
        "superseded_signed_sha256": str(row[10] or ""),
        "last_error": str(row[11] or ""),
        "updated_at": int(row[12] or 0),
    }


def automation_job_at(tree_size: int) -> dict | None:
    with _lock:
        return _automation_job_row(_connect(), int(tree_size))


def last_automation_job() -> dict | None:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT tree_size FROM anchor_automation_jobs ORDER BY updated_at DESC, tree_size DESC LIMIT 1"
        ).fetchone()
        return _automation_job_row(conn, int(row[0])) if row else None


def create_automation_job(
    *,
    tree_size: int,
    origin: str,
    root,
    checkpoint: str,
    request_id: str,
    params: dict,
    authorize_at: int,
) -> dict:
    """Persist exact sign intent before dialing the signer."""
    size = int(tree_size)
    root_hex = bytes(root).hex() if isinstance(root, (bytes, bytearray)) else str(root or "")
    when = int(authorize_at)
    params_text = json.dumps(dict(params or {}), sort_keys=True, separators=(",", ":"))
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = _automation_job_row(conn, size)
            if existing:
                same = (
                    existing["origin"] == str(origin or "")
                    and existing["root"] == root_hex
                    and existing["checkpoint"] == str(checkpoint or "")
                    and existing["request_id"] == str(request_id or "")
                    and existing["params"] == dict(params or {})
                    and existing["authorize_at"] == when
                )
                if not same:
                    raise StoreError("automation job conflict")
                conn.commit()
                return existing
            conn.execute(
                "INSERT INTO anchor_automation_jobs"
                "(tree_size, origin, root, checkpoint, request_id, params, authorize_at, "
                "status, resign_count, sign_attempts, superseded_signed_sha256, "
                "last_error, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, 0, '', '', ?)",
                (
                    size,
                    str(origin or ""),
                    root_hex,
                    str(checkpoint or ""),
                    str(request_id or ""),
                    params_text,
                    when,
                    when,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _automation_job_row(conn, size) or {}


def set_automation_job_status(
    tree_size: int, status: str, *, now: int, error: str = ""
) -> dict:
    want = str(status or "").strip()
    if want not in _AUTO_STATUS_RANK:
        raise StoreError("unknown automation status")
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = _automation_job_row(conn, int(tree_size))
            if not existing:
                raise StoreError("automation job missing")
            have = str(existing.get("status") or "")
            if have == "HALTED" and want != "HALTED":
                raise StoreError("halted automation job")
            if (
                want != "HALTED"
                and _AUTO_STATUS_RANK[want] < _AUTO_STATUS_RANK.get(have, -1)
            ):
                raise StoreError("automation status regression")
            conn.execute(
                "UPDATE anchor_automation_jobs SET status = ?, last_error = ?, updated_at = ? "
                "WHERE tree_size = ?",
                (want, str(error or "")[:120], int(now), int(tree_size)),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _automation_job_row(conn, int(tree_size)) or {}


def record_automation_sign_attempt(tree_size: int, *, now: int) -> dict:
    """Persist before IPC. Initial call plus one exact-request recovery only."""
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            job = _automation_job_row(conn, int(tree_size))
            if not job or job.get("status") != "PENDING":
                raise StoreError("automation sign state")
            if int(job.get("sign_attempts") or 0) >= 2:
                raise StoreError("automation sign attempt cap")
            conn.execute(
                "UPDATE anchor_automation_jobs SET sign_attempts = sign_attempts + 1, "
                "updated_at = ? WHERE tree_size = ?",
                (int(now), int(tree_size)),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _automation_job_row(conn, int(tree_size)) or {}


def begin_automation_resign(
    tree_size: int,
    *,
    request_id: str,
    params: dict,
    authorize_at: int,
) -> dict:
    """Allow one pre-POST policy refresh. Never after SEND_ATTEMPTED."""
    size = int(tree_size)
    when = int(authorize_at)
    params_text = json.dumps(dict(params or {}), sort_keys=True, separators=(",", ":"))
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            job = _automation_job_row(conn, size)
            auth = _authorized_row(conn, size)
            if not job or job.get("status") != "AUTHORIZED":
                raise StoreError("automation resign state")
            if int(job.get("resign_count") or 0) >= 1:
                raise StoreError("automation resign cap")
            if auth and send_state_rank(auth.get("send_state")) >= _LATCHED_RANK:
                raise StoreError("cannot resign after SEND_ATTEMPTED")
            prior_digest = (
                hashlib.sha256(bytes(auth.get("signed") or b"")).hexdigest()
                if auth and auth.get("signed")
                else ""
            )
            conn.execute("DELETE FROM authorized_anchors WHERE tree_size = ?", (size,))
            raw = conn.execute(
                "SELECT v FROM meta WHERE k = 'last_authorized_checkpoint'"
            ).fetchone()
            try:
                meta = json.loads(raw[0]) if raw and raw[0] else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                meta = {}
            if isinstance(meta, dict) and int(meta.get("size") or 0) == size:
                conn.execute(
                    "INSERT INTO meta(k, v) VALUES ('last_authorized_checkpoint', ?) "
                    "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                    (
                        json.dumps(
                            {
                                "size": 0,
                                "at": 0,
                                "request_id": "",
                                "origin": "",
                                "root": "",
                            }
                        ),
                    ),
                )
            conn.execute(
                "UPDATE anchor_automation_jobs SET request_id = ?, params = ?, authorize_at = ?, "
                "status = 'PENDING', resign_count = resign_count + 1, sign_attempts = 0, "
                "superseded_signed_sha256 = ?, last_error = '', updated_at = ? "
                "WHERE tree_size = ?",
                (str(request_id or ""), params_text, when, prior_digest, when, size),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _automation_job_row(conn, size) or {}


def reserve_automatic_send(
    *,
    tree_size: int,
    expected_txid: str,
    fee: int,
    now: int,
    hour_start: int,
    day_start: int,
    month_start: int,
    hourly_max: int,
    daily_max: int,
    monthly_max: int,
) -> dict:
    """Atomically reserve budgets and persist SEND_ATTEMPTED before POST."""
    size = int(tree_size)
    expected = str(expected_txid or "").strip()
    amount = int(fee)
    when = int(now)
    if not expected or amount < 1:
        raise StoreError("invalid automatic send reservation")
    with _lock:
        conn = _connect()
        try:
            # Serialize the read-check-write reservation across processes.
            # A contender must observe the committed row before it can POST.
            conn.execute("BEGIN IMMEDIATE")

            def fail(message: str) -> None:
                raise StoreError(message)

            existing_send = conn.execute(
                "SELECT expected_txid, fee FROM anchor_automation_sends "
                "WHERE tree_size = ?",
                (size,),
            ).fetchone()
            auth = _authorized_row(conn, size)
            job = _automation_job_row(conn, size)
            if existing_send:
                if str(existing_send[0]) != expected or int(existing_send[1]) != amount:
                    fail("automatic send conflict")
                fail("automatic send already reserved")
            if not auth or not auth.get("signed") or not job:
                fail("automatic authorization missing")
            if job.get("status") != "AUTHORIZED":
                fail("automatic job not authorized")
            if send_state_rank(auth.get("send_state")) >= _LATCHED_RANK:
                fail("automatic send already latched")
            hourly = int(
                conn.execute(
                    "SELECT COUNT(*) FROM anchor_automation_sends "
                    "WHERE attempted_at >= ?",
                    (int(hour_start),),
                ).fetchone()[0]
                or 0
            )
            if hourly >= int(hourly_max):
                fail("automatic hourly rate cap")
            daily = int(
                conn.execute(
                    "SELECT COALESCE(SUM(fee), 0) FROM anchor_automation_sends "
                    "WHERE attempted_at >= ?",
                    (int(day_start),),
                ).fetchone()[0]
                or 0
            )
            monthly = int(
                conn.execute(
                    "SELECT COALESCE(SUM(fee), 0) FROM anchor_automation_sends "
                    "WHERE attempted_at >= ?",
                    (int(month_start),),
                ).fetchone()[0]
                or 0
            )
            if daily + amount > int(daily_max):
                fail("automatic daily fee cap")
            if monthly + amount > int(monthly_max):
                fail("automatic monthly fee cap")
            conn.execute(
                "INSERT INTO anchor_automation_sends"
                "(tree_size, expected_txid, fee, attempted_at, status) "
                "VALUES (?, ?, ?, ?, 'SEND_ATTEMPTED')",
                (size, expected, amount, when),
            )
            conn.execute(
                "UPDATE authorized_anchors SET send_state = 'SEND_ATTEMPTED', "
                "expected_txid = ?, send_attempted_at = ? WHERE tree_size = ?",
                (expected, when, size),
            )
            conn.execute(
                "UPDATE anchor_automation_jobs SET status = 'SEND_ATTEMPTED', "
                "last_error = '', updated_at = ? WHERE tree_size = ?",
                (when, size),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        _chmod_db_files(_conn_path or db_path())
        return _authorized_row(conn, size) or {}


def set_automatic_send_status(tree_size: int, status: str, *, now: int) -> None:
    want = str(status or "").strip()
    if want not in {"SUBMITTED", "CONFIRMED"}:
        raise StoreError("invalid automatic send status")
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM anchor_automation_sends WHERE tree_size = ?",
                (int(tree_size),),
            ).fetchone()
            if not row:
                raise StoreError("automatic send missing")
            ranks = {"SEND_ATTEMPTED": 1, "SUBMITTED": 2, "CONFIRMED": 3}
            if ranks.get(str(row[0] or ""), -1) > ranks[want]:
                raise StoreError("automatic send status regression")
            conn.execute(
                "UPDATE anchor_automation_sends SET status = ? WHERE tree_size = ?",
                (want, int(tree_size)),
            )
            conn.execute(
                "UPDATE anchor_automation_jobs SET status = ?, updated_at = ? WHERE tree_size = ?",
                (want, int(now), int(tree_size)),
            )
            if want == "CONFIRMED":
                # Existing authorized_anchors/confirmed_anchors retain the
                # audit record. Keep only recent automatic budget metadata.
                cutoff = int(now) - (40 * 24 * 60 * 60)
                conn.execute(
                    "DELETE FROM anchor_automation_sends "
                    "WHERE status = 'CONFIRMED' AND attempted_at < ?",
                    (cutoff,),
                )
                conn.execute(
                    "DELETE FROM anchor_automation_jobs "
                    "WHERE status = 'CONFIRMED' AND updated_at < ?",
                    (cutoff,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def automatic_budget_usage(*, hour_start: int, day_start: int, month_start: int) -> dict:
    with _lock:
        conn = _connect()
        hourly = int(
            conn.execute(
                "SELECT COUNT(*) FROM anchor_automation_sends WHERE attempted_at >= ?",
                (int(hour_start),),
            ).fetchone()[0]
            or 0
        )
        daily = int(
            conn.execute(
                "SELECT COALESCE(SUM(fee), 0) FROM anchor_automation_sends WHERE attempted_at >= ?",
                (int(day_start),),
            ).fetchone()[0]
            or 0
        )
        monthly = int(
            conn.execute(
                "SELECT COALESCE(SUM(fee), 0) FROM anchor_automation_sends WHERE attempted_at >= ?",
                (int(month_start),),
            ).fetchone()[0]
            or 0
        )
        return {"hourly_count": hourly, "daily_fee": daily, "monthly_fee": monthly}


def last_confirmed_checkpoint() -> dict:
    empty = {
        "size": 0,
        "at": 0,
        "txid": "",
        "round": 0,
        "root": "",
        "origin": "",
        "network": "",
        "genesis_id": "",
    }
    raw = meta_get("last_confirmed_checkpoint")
    empty = {
        "size": 0,
        "at": 0,
        "txid": "",
        "round": 0,
        "root": "",
        "origin": "",
        "network": "",
        "genesis_id": "",
    }
    if not raw:
        return dict(empty)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return dict(empty)
    if not isinstance(data, dict):
        return dict(empty)
    return {
        "size": int(data.get("size") or 0),
        "at": int(data.get("at") or 0),
        "txid": str(data.get("txid") or ""),
        "round": int(data.get("round") or data.get("confirmed_round") or 0),
        "root": str(data.get("root") or ""),
        "origin": str(data.get("origin") or ""),
        "network": str(data.get("network") or ""),
        "genesis_id": str(data.get("genesis_id") or ""),
    }


_CONFIRM_PROOF_HOSTS = {
    "tatum": "algorand-mainnet-indexer.gateway.tatum.io",
    "nownodes": "algo-index.nownodes.io",
}
_MAINNET_ORIGIN = "402signal.com/pq/log/mainnet-v1"
_MAINNET_GENESIS_ID = "mainnet-v1.0"


def confirmation_provider_proof() -> dict:
    """Return public-only provider proof metadata, or empty on corruption."""
    raw = meta_get("confirmation_provider_proof")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        version = int(data.get("version") or 0)
        tree_size = int(data.get("tree_size") or 0)
        verified_at = int(data.get("verified_at") or 0)
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    provider = str(data.get("provider") or "")
    host = str(data.get("host") or "")
    root = str(data.get("root") or "")
    txid = str(data.get("txid") or "")
    if not (
        version == 1
        and _CONFIRM_PROOF_HOSTS.get(provider) == host
        and str(data.get("network") or "") == "mainnet"
        and str(data.get("falcon_scheme") or "") == "f1"
        and tree_size >= 1
        and len(root) == 64
        and all(ch in "0123456789abcdef" for ch in root)
        and len(txid) == 52
        and all(ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for ch in txid)
        and verified_at >= 1
    ):
        return {}
    return {
        "version": 1,
        "provider": provider,
        "host": host,
        "network": "mainnet",
        "falcon_scheme": "f1",
        "tree_size": tree_size,
        "root": root,
        "txid": txid,
        "verified_at": verified_at,
    }


def save_confirmation_provider_proof(
    *,
    provider: str,
    host: str,
    tree_size: int,
    root,
    txid: str,
    verified_at: int,
) -> dict:
    """Durably record an exact verification of the latest MainNet anchor.

    This stores no credential or response body. The proof cannot be written
    for an arbitrary transaction: it must exactly match the durable latest
    confirmed checkpoint while the SQLite write lock is held.
    """
    name = str(provider or "")
    want_host = str(host or "")
    if _CONFIRM_PROOF_HOSTS.get(name) != want_host:
        raise StoreError("invalid confirmation provider proof")
    size = int(tree_size)
    root_hex = bytes(root).hex() if isinstance(root, (bytes, bytearray)) else str(root or "")
    want_txid = str(txid or "")
    when = int(verified_at)
    if not (
        size >= 1
        and len(root_hex) == 64
        and all(ch in "0123456789abcdef" for ch in root_hex)
        and len(want_txid) == 52
        and all(ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for ch in want_txid)
        and when >= 1
    ):
        raise StoreError("invalid confirmation provider proof")
    payload = {
        "version": 1,
        "provider": name,
        "host": want_host,
        "network": "mainnet",
        "falcon_scheme": "f1",
        "tree_size": size,
        "root": root_hex,
        "txid": want_txid,
        "verified_at": when,
    }
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT v FROM meta WHERE k = 'last_confirmed_checkpoint'"
            ).fetchone()
            latest = json.loads(str(row[0] or "")) if row and row[0] else {}
            if not isinstance(latest, dict):
                raise StoreError("confirmed checkpoint unavailable")
            if not (
                int(latest.get("size") or 0) == size
                and str(latest.get("root") or "") == root_hex
                and str(latest.get("txid") or "") == want_txid
                and str(latest.get("origin") or "") == _MAINNET_ORIGIN
                and str(latest.get("network") or "") == "mainnet"
                and str(latest.get("genesis_id") or "") == _MAINNET_GENESIS_ID
            ):
                raise StoreError("confirmation proof does not match latest checkpoint")
            conn.execute(
                "INSERT INTO meta(k, v) VALUES ('confirmation_provider_proof', ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        _chmod_db_files(_conn_path or db_path())
    return confirmation_provider_proof()


def save_confirmed_checkpoint(
    *,
    tree_size: int,
    origin: str,
    root,
    txid: str,
    confirmed_round: int,
    at: int,
    network: str = "",
    genesis_id: str = "",
) -> dict:
    """Persist CONFIRMED only. Does not write authorized. Does not POST.

    Invariants (transactional):
      - tree_size is monotonic vs last confirmed (strictly greater, or equal if identical)
      - origin/root match the authorized row for that size when one exists
      - txid is a 52-char Algorand txid; confirmed_round >= 1
      - conflicting re-save of the same size is rejected
      - identical re-save is idempotent
    """
    from live402.pq import algo_anchor

    size = int(tree_size)
    if size < 1:
        raise ConflictError("invalid tree size")
    root_hex = bytes(root).hex() if isinstance(root, (bytes, bytearray)) else str(root or "")
    if not root_hex or len(root_hex) != 64:
        raise ConflictError("invalid root")
    want_origin = origin or ""
    want_txid = str(txid or "").strip()
    if not algo_anchor._looks_like_txid(want_txid):
        raise ConflictError("invalid txid")
    rnd = int(confirmed_round)
    if rnd < 1:
        raise ConflictError("invalid confirmed_round")
    when = int(at)
    want_network, want_genesis = _normalize_confirmed_network(network, genesis_id, want_origin)
    with _lock:
        conn = _connect()
        last = last_confirmed_checkpoint()
        last_size = int(last.get("size") or 0)
        if last_size and size < last_size:
            raise ConflictError("size not monotonic")
        if last_size and size == last_size:
            same_core = (
                str(last.get("origin") or "") == want_origin
                and str(last.get("root") or "") == root_hex
                and str(last.get("txid") or "") == want_txid
                and int(last.get("round") or 0) == rnd
            )
            if same_core:
                have_net = str(last.get("network") or "")
                have_gen = str(last.get("genesis_id") or "")
                if have_net == want_network and have_gen == want_genesis:
                    return last
                if not have_net and not have_gen and (want_network or want_genesis):
                    conn.execute(
                        "UPDATE confirmed_anchors SET network = ?, genesis_id = ? "
                        "WHERE tree_size = ?",
                        (want_network, want_genesis, size),
                    )
                    payload = dict(last)
                    payload["network"] = want_network
                    payload["genesis_id"] = want_genesis
                    conn.execute(
                        "INSERT INTO meta(k, v) VALUES ('last_confirmed_checkpoint', ?) "
                        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                        (json.dumps(payload),),
                    )
                    conn.commit()
                    return last_confirmed_checkpoint()
            raise ConflictError("confirmed checkpoint conflict")
        existing = conn.execute(
            "SELECT origin, root, txid, confirmed_round, network, genesis_id "
            "FROM confirmed_anchors WHERE tree_size = ?",
            (size,),
        ).fetchone()
        if existing:
            have_net = str(existing[4] or "") if len(existing) > 4 else ""
            have_gen = str(existing[5] or "") if len(existing) > 5 else ""
            same_core = (
                str(existing[0] or "") == want_origin
                and str(existing[1] or "") == root_hex
                and str(existing[2] or "") == want_txid
                and int(existing[3] or 0) == rnd
            )
            if same_core and have_net == want_network and have_gen == want_genesis:
                return last_confirmed_checkpoint()
            if same_core and not have_net and not have_gen and (want_network or want_genesis):
                conn.execute(
                    "UPDATE confirmed_anchors SET network = ?, genesis_id = ? "
                    "WHERE tree_size = ?",
                    (want_network, want_genesis, size),
                )
                payload = {
                    "size": size,
                    "at": when,
                    "txid": want_txid,
                    "round": rnd,
                    "root": root_hex,
                    "origin": want_origin,
                    "network": want_network,
                    "genesis_id": want_genesis,
                }
                conn.execute(
                    "INSERT INTO meta(k, v) VALUES ('last_confirmed_checkpoint', ?) "
                    "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                    (json.dumps(payload),),
                )
                conn.commit()
                return last_confirmed_checkpoint()
            raise ConflictError("confirmed checkpoint conflict")
        auth = _authorized_row(conn, size)
        if auth:
            if auth.get("origin") and want_origin and auth["origin"] != want_origin:
                raise ConflictError("origin mismatch")
            if auth.get("root") and root_hex and auth["root"] != root_hex:
                raise ConflictError("root mismatch")
        conn.execute(
            "INSERT INTO confirmed_anchors"
            "(tree_size, origin, root, txid, confirmed_round, at, network, genesis_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (size, want_origin, root_hex, want_txid, rnd, when, want_network, want_genesis),
        )
        payload = {
            "size": size,
            "at": when,
            "txid": want_txid,
            "round": rnd,
            "root": root_hex,
            "origin": want_origin,
            "network": want_network,
            "genesis_id": want_genesis,
        }
        conn.execute(
            "INSERT INTO meta(k, v) VALUES ('last_confirmed_checkpoint', ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (json.dumps(payload),),
        )
        conn.commit()
        _chmod_db_files(_conn_path or db_path())
        return last_confirmed_checkpoint()


def confirmed_anchor_count() -> int:
    """SELECT COUNT(*) from confirmed_anchors. Does not scan leaf bodies."""
    with _lock:
        conn = _connect()
        cur = conn.execute("SELECT COUNT(*) FROM confirmed_anchors")
        row = cur.fetchone()
        return int(row[0] or 0) if row else 0


def list_confirmed_anchors(limit: int = 250) -> list[dict]:
    """Persisted confirmed rows, newest first. Bounded. Does not invent rows."""
    cap = int(limit)
    if cap < 1:
        return []
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "SELECT tree_size, origin, root, txid, confirmed_round, at, network, genesis_id "
                "FROM confirmed_anchors ORDER BY at DESC, tree_size DESC LIMIT ?",
                (cap,),
            )
            fetched = cur.fetchall()
            wide = True
        except sqlite3.OperationalError:
            cur = conn.execute(
                "SELECT tree_size, origin, root, txid, confirmed_round, at "
                "FROM confirmed_anchors ORDER BY at DESC, tree_size DESC LIMIT ?",
                (cap,),
            )
            fetched = cur.fetchall()
            wide = False
        rows = []
        for row in fetched:
            tree_size, origin, root, txid, confirmed_round, at = row[:6]
            network = str(row[6] or "") if wide and len(row) > 6 else ""
            genesis_id = str(row[7] or "") if wide and len(row) > 7 else ""
            rows.append(
                {
                    "size": int(tree_size or 0),
                    "origin": str(origin or ""),
                    "root": str(root or ""),
                    "txid": str(txid or ""),
                    "round": int(confirmed_round or 0),
                    "at": int(at or 0),
                    "network": str(network or ""),
                    "genesis_id": str(genesis_id or ""),
                }
            )
        return rows
