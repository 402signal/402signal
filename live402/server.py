"""Tiny stdlib HTTP server for 402Signal. Port 8081 — AnalogPair stays on 8080."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from live402 import catalog, discover, history, mcp, payment, pulse, rails, validate
from live402 import http_body
from live402.http_body import BodyReadError
from live402.route import handle_route

STATIC_DIR = Path(__file__).resolve().parent / "static"
# Human pages served as static HTML from STATIC_DIR. Same CSP as GET /.
HUMAN_PAGES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/catalog": "catalog.html",
    "/catalog.html": "catalog.html",
    "/how": "how.html",
    "/how.html": "how.html",
    "/developers": "developers.html",
    "/developers.html": "developers.html",
}
# Server-rendered human pages. Intercept before static rewrite. Not STATIC_DIR files.
HUMAN_DYNAMIC_PATHS = frozenset({"/transparency", "/transparency.html"})
STATIC_FILES = {"/styles.css", "/app.js", "/dashboard.js", "/transparency.js"}
# Process-local volume files. Never HTTP-download, never static, never OpenAPI.
_VOLUME_DUMP_PATHS = frozenset(
    {
        "/catalog.sqlite",
        "/catalog.sqlite-wal",
        "/catalog.sqlite-shm",
        "/data",
        "/data/",
        "/data/catalog.sqlite",
        "/data/catalog.sqlite-wal",
        "/data/catalog.sqlite-shm",
        "/data/live402-history.sqlite",
        "/data/live402-history.sqlite-wal",
        "/data/live402-history.sqlite-shm",
        "/data/pq-log.sqlite",
        "/data/pq-log.sqlite-wal",
        "/data/pq-log.sqlite-shm",
        "/pq-log.sqlite",
        "/live402-history.sqlite",
        "/catalog/dump",
        "/catalog/export",
        "/dump",
        "/download/catalog",
    }
)
MAX_BODY = http_body.MAX_BODY
DEFAULT_MAX_HANDLERS = 32
REQUEST_TIMEOUT = http_body.BODY_READ_TIMEOUT
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
_HANDLER_SEMA_LOCK = threading.Lock()
_HANDLER_SEMA: threading.BoundedSemaphore | None = None
_HANDLER_SEMA_CAP = 0


def max_handlers() -> int:
    raw = (os.environ.get("LIVE402_MAX_HANDLERS") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            return DEFAULT_MAX_HANDLERS
    return DEFAULT_MAX_HANDLERS


def _handler_sema() -> threading.BoundedSemaphore:
    global _HANDLER_SEMA, _HANDLER_SEMA_CAP
    cap = max_handlers()
    with _HANDLER_SEMA_LOCK:
        if _HANDLER_SEMA is None or _HANDLER_SEMA_CAP != cap:
            _HANDLER_SEMA = threading.BoundedSemaphore(cap)
            _HANDLER_SEMA_CAP = cap
        return _HANDLER_SEMA


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip() in {"1", "true", "TRUE", "yes"}


def is_loopback_bind(host: str) -> bool:
    text = (host or "").strip().lower()
    if text in {"127.0.0.1", "::1", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def is_public_http_bind(host: str) -> bool:
    """True for PORT/Fly-style or non-loopback binds."""
    if os.environ.get("PORT"):
        return True
    text = (host or "").strip()
    if not text:
        return True
    if text in {"0.0.0.0", "::", "[::]"}:
        return True
    return not is_loopback_bind(text)


def assert_safe_http_boot(host: str) -> None:
    """Refuse public production-style servers with LOCAL_FREE or LIVE402_FIXTURE."""
    if not is_public_http_bind(host):
        return
    if not (_env_flag("LOCAL_FREE") or _env_flag("LIVE402_FIXTURE")):
        return
    if _env_flag("LIVE402_ALLOW_UNSAFE_DEV_MODE"):
        return
    raise SystemExit(
        "refusing public bind with LOCAL_FREE or LIVE402_FIXTURE; "
        "set LIVE402_ALLOW_UNSAFE_DEV_MODE=1 only for local use"
    )


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


def is_private_store_path(path: str) -> bool:
    """True for volume sqlite / dump URLs. Those stay process-local."""
    raw = (path or "").split("?", 1)[0].split("#", 1)[0]
    try:
        raw = urlparse(raw).path or raw
    except Exception:
        pass
    low = raw.lower().rstrip()
    if low in _VOLUME_DUMP_PATHS:
        return True
    if low.startswith("/data/"):
        return True
    if low.endswith(".sqlite") or low.endswith(".sqlite-wal") or low.endswith(".sqlite-shm"):
        return True
    return False


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def setup(self) -> None:
        super().setup()
        try:
            self.connection.settimeout(REQUEST_TIMEOUT)
        except Exception:
            pass

    def handle(self) -> None:
        sema = _handler_sema()
        if not sema.acquire(blocking=False):
            self._reject_saturated()
            return
        try:
            self.close_connection = True
            try:
                self.handle_one_request()
                while not self.close_connection:
                    self.handle_one_request()
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                self.close_connection = True
            if self.close_connection:
                self._shutdown_client()
        finally:
            sema.release()

    def _shutdown_client(self) -> None:
        """FIN the TCP socket so unread POST bytes cannot become the next request."""
        self.close_connection = True
        conn = getattr(self, "connection", None)
        if conn is None:
            return
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _reject_saturated(self) -> None:
        self.close_connection = True
        try:
            body = b'{"error":"server busy"}'
            self.connection.sendall(
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
                b"Connection: close\r\n"
                b"Cache-Control: no-store\r\n"
                b"\r\n" + body
            )
        except Exception:
            pass
        self._shutdown_client()

    def _close_error(self, code: int, error: str) -> None:
        self.close_connection = True
        try:
            self._json(code, {"error": error}, {"Connection": "close"})
            try:
                self.wfile.flush()
            except Exception:
                pass
        finally:
            # Close now, not after handle() returns. Unread framing bytes stay discarded.
            self._shutdown_client()

    def _read_json_body(self) -> dict | None:
        try:
            return http_body.read_json_object(self, max_body=MAX_BODY)
        except BodyReadError as exc:
            self._close_error(exc.status, exc.error)
            return None

    def translate_path(self, path: str) -> str:
        """Never map a request onto /data or a sqlite file."""
        check = (path or "").split("?", 1)[0].split("#", 1)[0]
        if is_private_store_path(check):
            return str(STATIC_DIR / ".__denied__")
        return SimpleHTTPRequestHandler.translate_path(self, path)

    def _deny_private_store(self) -> bool:
        parsed = urlparse(self.path)
        if not is_private_store_path(parsed.path):
            return False
        if getattr(self, "_omit_body", False) or getattr(self, "command", "") == "HEAD":
            self.send_response(404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()
            return True
        self._json(404, {"error": "not found"})
        return True

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

    def _bytes(self, code: int, body: bytes, content_type: str, extra_headers: dict | None = None) -> None:
        headers = {"Cache-Control": "no-store"}
        headers.update(extra_headers or {})
        self.send_response(code)
        self.send_header("Content-Type", content_type)
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

    def _close_unread_body(self) -> None:
        """Do not read leftover POST bytes. Caller must write then hard-close."""
        self.close_connection = True

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


    def _rewrite_static_path(self) -> bool:
        parsed = urlparse(self.path)
        page = HUMAN_PAGES.get(parsed.path)
        if page:
            self.path = "/" + page
            return True
        return parsed.path in STATIC_FILES

    def do_HEAD(self) -> None:
        if self._deny_private_store():
            return
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
        if parsed.path in HUMAN_DYNAMIC_PATHS:
            self._omit_body = True
            try:
                return self._html(200, self._transparency_html(), {"Cache-Control": "no-store"})
            finally:
                self._omit_body = False
        injected = self._homepage_html()
        if injected is not None:
            self._omit_body = True
            try:
                return self._html(200, injected)
            finally:
                self._omit_body = False
        if self._rewrite_static_path():
            return SimpleHTTPRequestHandler.do_HEAD(self)
        if parsed.path in head_ok or parsed.path.startswith("/pq/log/"):
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

    def _homepage_html(self) -> str | None:
        """Inject homepage PQ card only when last_confirmed.size > 0."""
        parsed = urlparse(self.path)
        if parsed.path not in ("/", "/index.html"):
            return None
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        try:
            from live402.pq import worker as pq_worker

            section = pq_worker.homepage_pq_html()
        except Exception:
            section = ""
        if not section:
            return None
        marker = "</main>"
        if marker not in html:
            return html
        return html.replace(marker, section + "    </main>", 1)

    def _transparency_html(self) -> str:
        from live402.pq import transparency as pq_view

        return pq_view.render_html()

    def do_GET(self) -> None:
        if self._deny_private_store():
            return
        parsed = urlparse(self.path)
        if parsed.path in HUMAN_DYNAMIC_PATHS:
            return self._html(200, self._transparency_html(), {"Cache-Control": "no-store"})
        if parsed.path in HUMAN_PAGES or parsed.path in STATIC_FILES:
            injected = self._homepage_html()
            if injected is not None:
                return self._html(200, injected)
            self._rewrite_static_path()
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
            networks: list[str] = []
            for raw in qs.get("networks") or []:
                networks.extend(part.strip() for part in str(raw).split(",") if part.strip())
            return self._json(
                200,
                pulse.preview_need(need, prefer_network=prefer, networks=networks or None),
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
        if parsed.path == "/pq/log" or parsed.path.startswith("/pq/log/"):
            if not self._public_allowed("pqlog"):
                return self._json(429, {"error": "rate limit"})
            from live402.pq import http as pq_http

            code, body, ctype, extra = pq_http.handle(parsed.path)
            return self._bytes(code, body, ctype, extra)
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
        if self._deny_private_store():
            self._close_unread_body()
            self._shutdown_client()
            return
        parsed = urlparse(self.path)
        if parsed.path in {"/mcp", "/mcp.json"}:
            return self._post_mcp()
        if parsed.path == "/validate":
            return self._post_validate()
        if parsed.path != "/route":
            self._close_unread_body()
            return self._close_error(404, "not found")
        if not self._route_allowed():
            self._close_unread_body()
            return self._close_error(429, "rate limit")
        payload = self._read_json_body()
        if payload is None:
            return
        code, body, extra = handle_route(payload, self.headers, self._resource_url())
        if extra is None and code == 402:
            extra = {"PAYMENT-REQUIRED": payment.payment_required_header(body)}
        return self._json(code, body, extra)

    def _post_mcp(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        if mcp.is_paid_call(payload) and not self._route_allowed():
            return self._close_error(429, "rate limit")
        if mcp.is_preview_call(payload) and not self._preview_allowed():
            return self._close_error(429, "rate limit")
        if mcp.is_validate_call(payload) and not self._validate_allowed():
            return self._close_error(429, "rate limit")
        code, body, extra = mcp.handle_mcp(payload, self.headers, self._mcp_resource_url())
        if extra is None and code == 402:
            extra = {"PAYMENT-REQUIRED": payment.payment_required_header(body)}
        return self._json(code, body, extra)


    def _post_validate(self) -> None:
        if not self._validate_allowed():
            self._close_unread_body()
            return self._close_error(429, "rate limit")
        payload = self._read_json_body()
        if payload is None:
            return
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


def boot_optional_log_signer() -> None:
    """Load LIVE402_PQ_LOG_SK into memory if set. Never generate a key.

    Malformed secret fails closed (no signer). /route still serves.
    Never logs or prints the secret.
    """
    from live402.pq import receipt as pq_receipt

    pq_receipt.load_signer_from_env()


def boot_http_process() -> None:
    """HTTP process boot: log signer only. No Algorand SK load."""
    boot_optional_log_signer()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve 402Signal locally")
    parser.add_argument("--host", default=default_host())
    parser.add_argument("--port", type=int, default=default_port())
    args = parser.parse_args(argv)
    assert_safe_http_boot(args.host)
    boot_http_process()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    # Existing production loop (catalog trickle + PQ tick / confirm).
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
