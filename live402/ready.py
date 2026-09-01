"""Readiness checks. No secrets, no filesystem paths in the response."""

from __future__ import annotations


def _check_sqlite(connect_fn) -> bool:
    try:
        conn = connect_fn()
        row = conn.execute("SELECT 1").fetchone()
        return bool(row and int(row[0]) == 1)
    except Exception:
        return False


def _catalog_ok() -> bool:
    try:
        from live402 import shadow

        shadow._connect()
        return _check_sqlite(shadow._connect)
    except Exception:
        return False


def _history_ok() -> bool:
    try:
        from live402 import history

        return _check_sqlite(history._connect)
    except Exception:
        return False


def _pq_log_sqlite_ok() -> bool:
    try:
        from live402.pq import store

        return _check_sqlite(store._connect)
    except Exception:
        return False


def _pq_log_ok() -> bool:
    """Sqlite reachable and not LOCAL LOG INCONSISTENT (size < last_confirmed).

    Uses monitor.ready_flags(), the boolean subset of the operator snapshot.
    GET /ready never returns the snapshot itself.
    """
    if not _pq_log_sqlite_ok():
        return False
    try:
        from live402.pq import monitor

        flags = monitor.ready_flags()
        return bool(flags.get("pq_log_sqlite")) and bool(flags.get("pq_log_integrity"))
    except Exception:
        return False


def _storage_ok() -> bool:
    """Writable sqlite journals for the three process-local databases."""
    return _catalog_ok() and _history_ok() and _pq_log_sqlite_ok()


def readiness() -> dict:
    """Public /ready body. Booleans only. Never paths, never env, never keys."""
    checks = {
        "storage": _storage_ok(),
        "catalog": _catalog_ok(),
        "history": _history_ok(),
        "pq_log": _pq_log_ok(),
    }
    return {"ok": all(checks.values()), "checks": checks}
