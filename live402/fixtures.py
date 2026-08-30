"""Offline catalog + canned probes. AnalogPair-style: LIVE402_FIXTURE=1, no network."""

from __future__ import annotations

import json
import os
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
FIXTURE_PATH = DATA_DIR / "fixtures.json"


def fixture_mode() -> bool:
    return os.environ.get("LIVE402_FIXTURE", "").strip() in {"1", "true", "TRUE", "yes"}


def local_free() -> bool:
    return os.environ.get("LOCAL_FREE", "").strip() in {"1", "true", "TRUE", "yes"}


def load_resources() -> list[dict]:
    payload = json.loads(FIXTURE_PATH.read_text())
    return list(payload.get("resources") or [])


def lookup_url(url: str) -> dict | None:
    url = (url or "").strip()
    for row in load_resources():
        if row.get("url") == url:
            return row
    return None
