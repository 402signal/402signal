"""Tiny stdlib HTTP server for 402Signal. Port 8081 — AnalogPair stays on 8080."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from live402 import catalog, discover, history, mcp, payment, pulse, rails, validate
from live402.route import handle_route

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 64_000
DEFAULT_ROUTE_RPM = 60
DEFAULT_PREVIEW_RPM = 180
DEFAULT_PUBLIC_RPM = 180
DEFAULT_VALIDATE_RPM = 60
FACILITATOR_ROUTE_RPM = 180
HSTS = "max-age=31536000"
# script-src 'self' only (no vendor wallet scripts, no CDN).
# connect-src is 'self' only. Homepage Base pay POSTs /route; no WalletConnect.
CSP = (
    "default-src 'none'; script-src 'self'; "
    "connect-src 'self'; "
    "style-src 'self'; img-src 'self' data:; base-uri 'self'; "
    "frame-ancestors 'none'"
)
FACILITATOR_UA = (
    "coinbase",
    "cdp",
    "payai",
    "goplausible",
    "x402",
    "bazaar",
)
_PAYMENT_LOG_RE = re.compile(
    r"(?i)\b(PAYMENT-SIGNATURE|PAYMENT-PAYLOAD|X-PAYMENT(?:-SIGNATURE)?"
    r"|PAYMENT-RESPONSE|PAYMENT-REQUIRED)\b\s*[:=]?\s*\S+"
)


class _RateLimiter:
    """In-memory sliding window. Fail closed on errors."""

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        try:
            cap = int(limit)
            if cap < 1:
                return False
            now = time.monotonic()
            with self._lock:
                hits = [t for t in (self._hits.get(key) or []) if now - t < window]
                if len(hits) >= cap:
                    self._hits[key] = hits
                    return False
                hits.append(now)
                self._hits[key] = hits
                return True
        except Exception:
            return False


_ROUTE_LIMITER = _RateLimiter()
_PREVIEW_LIMITER = _RateLimiter()
_PUBLIC_LIMITER = _RateLimiter()
_VALIDATE_LIMITER = _RateLimiter()


def route_rpm() -> int:
    raw = (os.environ.get("LIVE402_ROUTE_RPM") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            return DEFAULT_ROUTE_RPM
    return DEFAULT_ROUTE_RPM


def facilitator_rpm() -> int:
    raw = (os.environ.get("LIVE402_ROUTE_RPM_FACILITATOR") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            return FACILITATOR_ROUTE_RPM
    return max(FACILITATOR_ROUTE_RPM, route_rpm())


def preview_rpm() -> int:
    raw = (os.environ.get("LIVE402_PREVIEW_RPM") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    # Crawlers and unpaid MCP preview must stay looser than paid POST /route.
    return max(DEFAULT_PREVIEW_RPM, route_rpm() * 2)


def public_rpm() -> int:
    raw = (os.environ.get("LIVE402_PUBLIC_RPM") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    # GET /pulse and GET /rails: looser than paid /route so crawlers do not look dead.
    return max(DEFAULT_PUBLIC_RPM, route_rpm() * 2)


def validate_rpm() -> int:
    raw = (os.environ.get("LIVE402_VALIDATE_RPM") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return DEFAULT_VALIDATE_RPM


def _is_facilitator_ua(ua: str) -> bool:
    low = (ua or "").lower()
    return any(marker in low for marker in FACILITATOR_UA)


def client_ip(handler: SimpleHTTPRequestHandler) -> str:
    fly = (handler.headers.get("Fly-Client-IP") or "").split(",")[0].strip()
    if fly:
        return fly
    xff = (handler.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if xff:
        return xff
    return handler.client_address[0] if handler.client_address else "unknown"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        try:
            text = fmt % args
        except Exception:
            text = "%s %s" % (fmt, " ".join(str(a) for a in args))
        text = _PAYMENT_LOG_RE.sub(r"\1=[redacted]", text)
        sys.stderr.write("%s - %s\n" % (self.address_string(), text))

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        # Fly terminates TLS; browsers ignore HSTS on plain HTTP.
        self.send_header("Strict-Transport-Security", HSTS)
        self.send_header("Content-Security-Policy", CSP)
        super().end_headers()

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, PAYMENT-SIGNATURE, PAYMENT-PAYLOAD, X-PAYMENT, PAYMENT-RESPONSE, Algorand-Sender, X-Algorand-Sender",
        )
        self.send_header(
            "Access-Control-Expose-Headers",
            "PAYMENT-REQUIRED, PAYMENT-RESPONSE",
        )

    def _json(self, code: int, payload: dict, extra_headers: dict | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Cache-Control": "no-store"}
        headers.update(extra_headers or {})
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        for key, val in headers.items():
            self.send_header(key, val)
        self.end_headers()
        if not getattr(self, "_omit_body", False):
            self.wfile.write(body)

    def _text(self, code: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=300")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        if not getattr(self, "_omit_body", False):
            self.wfile.write(data)

    def _html(self, code: int, body: str, extra_headers: dict | None = None) -> None:
        data = body.encode("utf-8")
        headers = {"Cache-Control": "public, max-age=15"}
        headers.update(extra_headers or {})
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        for key, val in headers.items():
            self.send_header(key, val)
        self.end_headers()
        if not getattr(self, "_omit_body", False):
            self.wfile.write(data)

    def _wants_html(self) -> bool:
        """Browsers send text/html. Agents, curl, and crawlers get JSON 402."""
        accept = (self.headers.get("Accept") or "").lower()
        return "text/html" in accept

    def _resource_url(self) -> str:
        # Pinned public origin. Do not reflect Host (fly.dev / spoofed Host).
        return discover.ROUTE

    def _origin(self) -> str:
        return discover.ORIGIN

    def _mcp_resource_url(self) -> str:
        return discover.ORIGIN + "/mcp"

    def _discard_body(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 0:
            self.rfile.read(min(length, MAX_BODY))

    def _route_allowed(self) -> bool:
        ip = client_ip(self)
        ua = self.headers.get("User-Agent") or ""
        limit = facilitator_rpm() if _is_facilitator_ua(ua) else route_rpm()
        return _ROUTE_LIMITER.allow(ip, limit)

    def _preview_allowed(self) -> bool:
        ip = client_ip(self)
        return _PREVIEW_LIMITER.allow(ip, preview_rpm())

    def _public_allowed(self, which: str) -> bool:
        ip = client_ip(self)
        return _PUBLIC_LIMITER.allow("%s:%s" % (ip, which), public_rpm())

    def _validate_allowed(self) -> bool:
        ip = client_ip(self)
        return _VALIDATE_LIMITER.allow(ip, validate_rpm())

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()


    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        head_ok = {
            "/llms.txt",
            "/openapi.json",
            "/mcp.json",
            "/preview",
            "/rails",
            "/pulse",
            "/attestation",
        }
        static_ok = {"/", "/index.html", "/styles.css", "/app.js", "/dashboard.js"}
        if parsed.path in static_ok:
            return SimpleHTTPRequestHandler.do_HEAD(self)
        if parsed.path in head_ok:
            self._omit_body = True
            try:
                return self.do_GET()
            finally:
                self._omit_body = False
        self.send_response(404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            return SimpleHTTPRequestHandler.do_GET(self)
        if parsed.path in {"/styles.css", "/app.js", "/dashboard.js"}:
            return SimpleHTTPRequestHandler.do_GET(self)
        if parsed.path == "/route":
            allow = {"Allow": "GET, POST, OPTIONS"}
            if self._wants_html():
                html = (STATIC_DIR / "route.html").read_text(encoding="utf-8")
                return self._html(200, html, extra_headers=allow)
            sender = self.headers.get("Algorand-Sender") or self.headers.get("X-Algorand-Sender")
            required = payment.payment_required(self._resource_url(), algorand_sender=sender)
            extra = dict(allow)
            extra["PAYMENT-REQUIRED"] = payment.payment_required_header(required)
            return self._json(402, required, extra)
        if parsed.path == "/health":
            return self._json(200, {"ok": True})
        if parsed.path == "/preview":
            if not self._preview_allowed():
                return self._json(429, {"error": "rate limit"})
            qs = parse_qs(parsed.query)
            need = (qs.get("need") or [""])[0]
            prefer = (qs.get("prefer_network") or [""])[0]
            return self._json(
                200,
                pulse.preview_need(need, prefer_network=prefer),
                extra_headers={"Cache-Control": "no-store"},
            )
        if parsed.path == "/rails":
            if not self._public_allowed("rails"):
                return self._json(429, {"error": "rate limit"})
            return self._json(
                200,
                rails.get_rails(),
                extra_headers={"Cache-Control": "public, max-age=15"},
            )
        if parsed.path == "/pulse":
            if not self._public_allowed("pulse"):
                return self._json(429, {"error": "rate limit"})
            # Query string is ignored on purpose — never fetch caller URLs.
            return self._json(
                200,
                pulse.get_pulse(),
                extra_headers={"Cache-Control": "no-store"},
            )
        if parsed.path == "/attestation":
            if not self._public_allowed("attestation"):
                return self._json(429, {"error": "rate limit"})
            qs = parse_qs(parsed.query)
            batch_id = (qs.get("batch_id") or [""])[0]
            payload = history.attestation_for(batch_id or None)
            if not payload:
                return self._json(404, {"error": "no_batch"})
            return self._json(
                200,
                payload,
                extra_headers={"Cache-Control": "no-store"},
            )
        if parsed.path == "/validate":
            if not self._validate_allowed():
                return self._json(429, {"error": "rate limit"})
            qs = parse_qs(parsed.query)
            url = (qs.get("url") or [""])[0]
            code, body = validate.validate_url(url)
            return self._json(code, body, extra_headers={"Cache-Control": "no-store"})
        if parsed.path in {"/dashboard", "/dashboard.html"}:
            return self._html(
                200,
                pulse.dashboard_html(),
                extra_headers={"Cache-Control": "no-store"},
            )
        if parsed.path == "/openapi.json":
            return self._json(
                200,
                discover.openapi_spec(self._resource_url()),
                extra_headers={"Cache-Control": "public, max-age=300"},
            )
        if parsed.path in {"/.well-known/x402", "/.well-known/x402.json"}:
            return self._json(
                200,
                discover.well_known(self._resource_url()),
                extra_headers={"Cache-Control": "public, max-age=300"},
            )
        if parsed.path == "/robots.txt":
            return self._text(200, discover.ROBOTS_TXT)
        if parsed.path == "/llms.txt":
            return self._text(200, discover.LLMS_TXT)
        if parsed.path in {"/mcp", "/mcp.json", "/.well-known/mcp.json"}:
            return self._json(
                200,
                mcp.manifest(),
                extra_headers={"Cache-Control": "public, max-age=300"},
            )
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/mcp", "/mcp.json"}:
            return self._post_mcp()
        if parsed.path == "/validate":
            return self._post_validate()
        if parsed.path != "/route":
            return self._json(404, {"error": "not found"})
        if not self._route_allowed():
            self._discard_body()
            return self._json(429, {"error": "rate limit"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "body too large"})
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json(400, {"error": "invalid JSON"})
        if not isinstance(payload, dict):
            return self._json(400, {"error": "JSON object required"})
        code, body, extra = handle_route(payload, self.headers, self._resource_url())
        if extra is None and code == 402:
            extra = {"PAYMENT-REQUIRED": payment.payment_required_header(body)}
        return self._json(code, body, extra)

    def _post_mcp(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "body too large"})
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json(400, {"error": "invalid JSON"})
        if not isinstance(payload, dict):
            return self._json(400, {"error": "JSON object required"})
        if mcp.is_paid_call(payload) and not self._route_allowed():
            return self._json(429, {"error": "rate limit"})
        if mcp.is_preview_call(payload) and not self._preview_allowed():
            return self._json(429, {"error": "rate limit"})
        if mcp.is_validate_call(payload) and not self._validate_allowed():
            return self._json(429, {"error": "rate limit"})
        code, body, extra = mcp.handle_mcp(payload, self.headers, self._mcp_resource_url())
        if extra is None and code == 402:
            extra = {"PAYMENT-REQUIRED": payment.payment_required_header(body)}
        return self._json(code, body, extra)


    def _post_validate(self) -> None:
        if not self._validate_allowed():
            self._discard_body()
            return self._json(429, {"error": "rate limit"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "body too large"})
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json(400, {"error": "invalid JSON"})
        if not isinstance(payload, dict):
            return self._json(400, {"error": "JSON object required"})
        url = payload.get("url")
        if url is not None and not isinstance(url, str):
            return self._json(400, {"error": "url must be a string", "miss_reason": "invalid_need"})
        code, body = validate.validate_url(url if isinstance(url, str) else "")
        return self._json(code, body, extra_headers={"Cache-Control": "no-store"})


def default_host() -> str:
    raw = os.environ.get("LIVE402_HOST")
    if raw and raw.strip():
        return raw.strip()
    # Fly / containers set PORT; bind all interfaces there.
    if os.environ.get("PORT"):
        return "0.0.0.0"
    return "127.0.0.1"


def default_port() -> int:
    for key in ("LIVE402_PORT", "PORT"):
        raw = os.environ.get(key)
        if raw and str(raw).strip():
            return int(raw)
    return 8081


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve 402Signal locally")
    parser.add_argument("--host", default=default_host())
    parser.add_argument("--port", type=int, default=default_port())
    args = parser.parse_args(argv)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    catalog.start_refresher()
    print(
        "402Signal http://%s:%s  fixture=%r local_free=%r"
        % (
            args.host,
            args.port,
            os.environ.get("LIVE402_FIXTURE", ""),
            os.environ.get("LOCAL_FREE", ""),
        ),
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        httpd.server_close()


if __name__ == "__main__":
    main()
