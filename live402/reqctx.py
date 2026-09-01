"""Per-request context. Request id only; never payment material."""

from __future__ import annotations

import contextvars

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
