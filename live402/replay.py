"""Process-local payment-authorization replay guard. Never persist raw payment material."""

from __future__ import annotations

import hashlib
import json
import threading

from live402 import clock, payment

COMPLETED_TTL_SECONDS = 120.0
MAX_COMPLETED = 2048
WAIT_SLICE = 0.05


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


class _Entry:
    __slots__ = ("event", "result")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: tuple | None = None


_lock = threading.Lock()
_inflight: dict[str, _Entry] = {}
_completed: dict[str, tuple[float, tuple]] = {}


def reset() -> None:
    with _lock:
        for entry in _inflight.values():
            entry.event.set()
        _inflight.clear()
        _completed.clear()


def _prune_completed(now: float) -> None:
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
    """Acquire execution, return a cached result, or an in-flight entry to wait on."""
    now = clock.monotonic()
    with _lock:
        _prune_completed(now)
        cached = _completed.get(fp)
        if cached and cached[0] > now:
            return "cached", cached[1]
        existing = _inflight.get(fp)
        if existing is not None:
            return "wait", existing
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
    """Publish the result to waiters. Cache settled/rejected fingerprints only."""
    now = clock.monotonic()
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


def abandon(fp: str) -> None:
    """Release in-flight without caching. Waiters fail closed unless a result was set."""
    with _lock:
        entry = _inflight.pop(fp, None)
        if entry is not None:
            entry.event.set()
