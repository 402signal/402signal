"""Process-local operator counters. No secrets, no filesystem paths.

Worker and ready/monitor both read this. Do not import worker here.
"""

from __future__ import annotations

import time

_last_error = ""
_last_error_at = 0
_db_errors = 0
_last_db_error = ""
_last_db_error_at = 0
_recovery_conflicts = 0
_last_recovery_conflict = ""
_last_recovery_conflict_at = 0
_non_pq1_incidents = 0
_last_non_pq1 = False


def reset() -> None:
    """Test helper. Clears in-memory counters only."""
    global _last_error, _last_error_at
    global _db_errors, _last_db_error, _last_db_error_at
    global _recovery_conflicts, _last_recovery_conflict, _last_recovery_conflict_at
    global _non_pq1_incidents, _last_non_pq1
    _last_error = ""
    _last_error_at = 0
    _db_errors = 0
    _last_db_error = ""
    _last_db_error_at = 0
    _recovery_conflicts = 0
    _last_recovery_conflict = ""
    _last_recovery_conflict_at = 0
    _non_pq1_incidents = 0
    _last_non_pq1 = False


def _now() -> int:
    return int(time.time())


def record_error(code: str) -> None:
    """Record a short error code. Never store secrets or paths."""
    global _last_error, _last_error_at
    text = str(code or "").strip()[:80]
    if not text:
        return
    _last_error = text
    _last_error_at = _now()


def record_db_error(code: str = "sqlite") -> None:
    global _db_errors, _last_db_error, _last_db_error_at
    _db_errors += 1
    _last_db_error = str(code or "sqlite").strip()[:80]
    _last_db_error_at = _now()
    record_error("db")


def record_recovery_conflict(code: str = "authorized_mismatch") -> None:
    global _recovery_conflicts, _last_recovery_conflict, _last_recovery_conflict_at
    _recovery_conflicts += 1
    _last_recovery_conflict = str(code or "authorized_mismatch").strip()[:80]
    _last_recovery_conflict_at = _now()
    record_error("recovery_conflict")


def record_non_pq1_incident() -> None:
    """Unexpected non-PQ1 activity on the Falcon account is an incident."""
    global _non_pq1_incidents, _last_non_pq1
    _non_pq1_incidents += 1
    _last_non_pq1 = True
    record_error("unexpected_non_pq1_txn")


def snapshot() -> dict:
    return {
        "last_error": _last_error,
        "last_error_at": _last_error_at,
        "db_errors": _db_errors,
        "last_db_error": _last_db_error,
        "last_db_error_at": _last_db_error_at,
        "recovery_conflicts": _recovery_conflicts,
        "last_recovery_conflict": _last_recovery_conflict,
        "last_recovery_conflict_at": _last_recovery_conflict_at,
        "non_pq1_incidents": _non_pq1_incidents,
        "last_non_pq1_incident": _last_non_pq1,
    }
