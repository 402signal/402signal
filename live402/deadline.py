"""One end-to-end paid-request deadline. Advertised timeout bounds the whole flow."""

from __future__ import annotations

from live402 import clock

# Advertised maxTimeoutSeconds on each accept. Not 20+55+45 sequential.
PAYMENT_TIMEOUT_SECONDS = 60.0
SETTLE_RESERVE_SECONDS = 10.0
VERIFY_CAP_SECONDS = 8.0
SETTLE_CAP_SECONDS = 10.0
MIN_SLICE_SECONDS = 0.05


def advertised_timeout(accept: dict | None = None) -> float:
    timeout = PAYMENT_TIMEOUT_SECONDS
    if isinstance(accept, dict):
        raw = accept.get("maxTimeoutSeconds")
        if raw is not None and raw != "":
            try:
                timeout = float(raw)
            except (TypeError, ValueError):
                timeout = PAYMENT_TIMEOUT_SECONDS
    if timeout < 1.0:
        timeout = 1.0
    if timeout > PAYMENT_TIMEOUT_SECONDS:
        timeout = PAYMENT_TIMEOUT_SECONDS
    return timeout


def payment_deadline(accept: dict | None = None, now: float | None = None) -> float:
    start = clock.monotonic() if now is None else float(now)
    return start + advertised_timeout(accept)


def remaining(deadline: float | None, now: float | None = None) -> float | None:
    if deadline is None:
        return None
    t = clock.monotonic() if now is None else float(now)
    return float(deadline) - t


def _clamp_slice(seconds: float) -> float:
    if seconds <= 0:
        return 0.0
    if seconds < MIN_SLICE_SECONDS:
        return MIN_SLICE_SECONDS
    return seconds


def verify_timeout(deadline: float, now: float | None = None) -> float:
    """Time for verify, leaving a settle reserve when possible."""
    left = remaining(deadline, now)
    if left is None:
        return VERIFY_CAP_SECONDS
    usable = left - SETTLE_RESERVE_SECONDS
    if usable <= 0:
        return _clamp_slice(left)
    return min(VERIFY_CAP_SECONDS, usable)


def probe_deadline(deadline: float) -> float:
    """Probe must finish before this so settle still has reserve."""
    return float(deadline) - SETTLE_RESERVE_SECONDS


def settle_timeout(deadline: float, now: float | None = None) -> float:
    left = remaining(deadline, now)
    if left is None:
        return SETTLE_CAP_SECONDS
    if left <= 0:
        return MIN_SLICE_SECONDS
    return min(SETTLE_CAP_SECONDS, left)
