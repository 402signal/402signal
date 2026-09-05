"""Operator-configured lab origins; never trust a caller's self-test label alone."""
from __future__ import annotations
import os
from urllib.parse import urlsplit

PROTOCOL = "402signal-lab-route-v1"

def origins() -> list[str]:
    out = []
    for raw in os.environ.get("LIVE402_LAB_ORIGINS", "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            u = urlsplit(raw)
            if (u.scheme != "https" or not u.hostname or u.username or u.password
                    or u.path or u.query or u.fragment or u.port not in (None, 443)
                    or raw != "https://" + u.netloc or len(raw) > 255):
                raise ValueError("invalid lab origin")
        except ValueError:
            # Invalid configuration never silently grants an exclusion.
            return []
        out.append("https://" + u.hostname)
    return sorted(set(out))[:16]

def is_lab_url(url) -> bool:
    if not isinstance(url, str):
        return False
    try:
        u = urlsplit(url.strip())
        return (not (u.username or u.password) and u.scheme == "https" and u.port in (None, 443)
                and u.hostname is not None and "https://" + u.hostname in origins())
    except ValueError:
        return False

def classification() -> dict:
    return {"protocol": PROTOCOL, "traffic_class": "self_test", "organic_demand": False,
            "history_promoted": False, "pq_recorded": False}

def advertise(required: dict) -> None:
    allowed = origins()
    if allowed:
        required["lab_testing"] = {"protocol": PROTOCOL, "origins": allowed,
                                   "history_promoted": False, "pq_recorded": False}
