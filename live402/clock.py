"""Patchable monotonic clock. Tests inject a fake clock here."""

from __future__ import annotations

import time


def monotonic() -> float:
    return time.monotonic()
