"""SQLite transparency log. Separate file from catalog.sqlite and history.sqlite.

leaves(idx PK, body BLOB, leaf_hash BLOB)
compact tree hashes
tiles(level, n, width, data)
entry_bundles (uint16 BE length + bytes)
meta origin/vkey/size/checkpoint
optional checkpoints(size PK, note TEXT)

Publish order: entry bundles → tiles → tree → signed checkpoint.
Never checkpoint a size whose tiles/bundles are missing.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable

from live402.pq import DEFAULT_DB, ORIGIN, VOLUME_DB
from live402.pq import merkle
from live402.pq import tiles as tilemod

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None

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
"""


def db_path() -> str:
    raw = (os.environ.get("LIVE402_PQ_LOG_DB") or "").strip()
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
        conn.execute("INSERT INTO meta(k, v) VALUES ('origin', ?)", (ORIGIN,))
        conn.execute("INSERT INTO meta(k, v) VALUES ('size', '0')")
        conn.execute("INSERT INTO meta(k, v) VALUES ('vkey', '')")
        conn.execute("INSERT INTO meta(k, v) VALUES ('checkpoint', '')")
        conn.commit()


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
    return meta_get("origin") or ORIGIN


def size() -> int:
    with _lock:
        return _size_unlocked(_connect())


def _size_unlocked(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM leaves")
    return int(cur.fetchone()[0] or 0)


def root(tree_size: int | None = None) -> bytes:
    with _lock:
        conn = _connect()
        hashes = _leaf_hashes_unlocked(conn, tree_size)
        return merkle.mth_from_leaf_hashes(hashes, get_range=lambda a, b: _cached_range(conn, a, b))


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
        _store_range(conn, idx, idx + 1, digest)
        new_size = idx + 1
        hashes = _leaf_hashes_unlocked(conn, new_size)
        root_h = merkle.mth_from_leaf_hashes(hashes)
        _store_range(conn, 0, new_size, root_h)
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
    with _lock:
        conn = _connect()
        if width is None:
            cur = conn.execute(
                "SELECT data FROM tiles WHERE level = ? AND n = ? AND width = ?",
                (level, n, tilemod.TILE_WIDTH),
            )
        else:
            cur = conn.execute(
                "SELECT data FROM tiles WHERE level = ? AND n = ? AND width = ?",
                (level, n, width),
            )
        row = cur.fetchone()
        return bytes(row[0]) if row else None


def get_entry_bundle(n: int, width: int | None = None) -> bytes | None:
    with _lock:
        conn = _connect()
        w = tilemod.TILE_WIDTH if width is None else int(width)
        cur = conn.execute(
            "SELECT data FROM entry_bundles WHERE n = ? AND width = ?",
            (n, w),
        )
        row = cur.fetchone()
        return bytes(row[0]) if row else None


def inclusion_path(index: int, tree_size: int | None = None) -> list[bytes]:
    with _lock:
        conn = _connect()
        hashes = _leaf_hashes_unlocked(conn, tree_size)
        return merkle.inclusion_path(index, hashes)


def consistency_path(old_size: int, new_size: int | None = None) -> list[bytes]:
    with _lock:
        conn = _connect()
        hashes = _leaf_hashes_unlocked(conn, new_size)
        return merkle.consistency_path(old_size, hashes)


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
