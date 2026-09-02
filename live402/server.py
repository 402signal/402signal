"""Tiny stdlib HTTP server for 402Signal. Port 8081 — AnalogPair stays on 8080."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
import threading
import time
import uuid
from collections import OrderedDict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from live402 import asset_version, catalog, discover, history, mcp, payment, pulse, rails, ready, reqctx, validate
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
    "/contact": "contact.html",
    "/contact.html": "contact.html",
}
# Server-rendered human pages. Intercept before static rewrite. Not STATIC_DIR files.
HUMAN_DYNAMIC_PATHS = frozenset({"/transparency", "/transparency.html"})
STATIC_FILES = {"/styles.css", "/app.js", "/dashboard.js", "/transparency.js"}
# Constant paths only. Never join a request string onto STATIC_DIR.
_ASSET_PATHS = {
    "/styles.css": STATIC_DIR / "styles.css",
    "/app.js": STATIC_DIR / "app.js",
    "/dashboard.js": STATIC_DIR / "dashboard.js",
    "/transparency.js": STATIC_DIR / "transparency.js",
}
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
RATE_LIMIT_MAX_KEYS = 4096
HSTS = "max-age=31536000"
# script-src 'self' only (no vendor wallet scripts, no CDN).
# connect-src is 'self' only. Homepage Base pay POSTs /route; no WalletConnect.
CSP = (
    "default-src 'none'; script-src 'self'; "
    "connect-src 'self'; "
    "style-src 'self'; img-src 'self' data:; base-uri 'self'; "
    "frame-ancestors 'none'"
)
class _RateLimiter:
    """In-memory sliding window with TTL/LRU bound. Fail closed on errors."""

    def __init__(self, max_keys: int = RATE_LIMIT_MAX_KEYS) -> None:
        self._hits: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_keys = max(1, int(max_keys))

    def _prune_key(self, key: str, now: float, window: float) -> list[float]:
        hits = [t for t in (self._hits.get(key) or []) if now - t < window]
        if hits:
            self._hits[key] = hits
            self._hits.move_to_end(key)
        else:
            self._hits.pop(key, None)
        return hits

    def _evict(self, now: float, window: float) -> None:
        for key in list(self._hits.keys()):
            self._prune_key(key, now, window)
            if len(self._hits) < self._max_keys:
                return
        while len(self._hits) >= self._max_keys:
            try:
                self._hits.popitem(last=False)
            except KeyError:
                break

    def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        try:
            cap = int(limit)
            if cap < 1:
                return False
            now = time.monotonic()
            with self._lock:
                hits = self._prune_key(key, now, window)
                if len(self._hits) >= self._max_keys and key not in self._hits:
                    self._evict(now, window)
                if len(hits) >= cap:
                    return False
                hits.append(now)
                self._hits[key] = hits
                self._hits.move_to_end(key)
                if len(self._hits) > self._max_keys:
                    self._evict(now, window)
                return True
        except Exception:
            return False

    def key_count(self) -> int:
        with self._lock:
            return len(self._hits)


_ROUTE_LIMITER = _RateLimiter()
_PREVIEW_LIMITER = _RateLimiter()
_PUBLIC_LIMITER = _RateLimiter()
_VALIDATE_LIMITER = _RateLimiter()
_HANDLER_SEMA_LOCK = threading.Lock()
_HANDLER_SEMA: threading.BoundedSemaphore | None = None
_HANDLER_SEMA_CAP = 0
_THREAD_STATS_LOCK = threading.Lock()
_ACTIVE_REQUEST_THREADS = 0
_PEAK_REQUEST_THREADS = 0


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


def request_thread_stats() -> tuple[int, int]:
    """(active worker threads, peak). Test helper for the server-level cap."""
    with _THREAD_STATS_LOCK:
        return _ACTIVE_REQUEST_THREADS, _PEAK_REQUEST_THREADS


def reset_request_thread_stats() -> None:
    global _ACTIVE_REQUEST_THREADS, _PEAK_REQUEST_THREADS
    with _THREAD_STATS_LOCK:
        _ACTIVE_REQUEST_THREADS = 0
        _PEAK_REQUEST_THREADS = 0


def reject_saturated_socket(sock) -> None:
    """HTTP 503 Connection: close, then SHUT_RDWR. No request thread is created."""
    body = b'{"error":"server busy"}'
    try:
        sock.sendall(
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n"
            b"Cache-Control: no-store\r\n"
            b"\r\n" + body
        )
    except Exception:
        pass
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Acquire a worker slot before spawning the request thread.

    ThreadingHTTPServer.process_request starts the thread before Handler.handle(),
    so a semaphore inside handle() cannot bound thread creation.
    """

    def process_request(self, request, client_address):
        sema = _handler_sema()
        if not sema.acquire(blocking=False):
            reject_saturated_socket(request)
            return

        def run() -> None:
            global _ACTIVE_REQUEST_THREADS, _PEAK_REQUEST_THREADS
            with _THREAD_STATS_LOCK:
                _ACTIVE_REQUEST_THREADS += 1
                if _ACTIVE_REQUEST_THREADS > _PEAK_REQUEST_THREADS:
                    _PEAK_REQUEST_THREADS = _ACTIVE_REQUEST_THREADS
            try:
                self.process_request_thread(request, client_address)
            finally:
                with _THREAD_STATS_LOCK:
                    _ACTIVE_REQUEST_THREADS = max(0, _ACTIVE_REQUEST_THREADS - 1)
                try:
                    sema.release()
                except Exception:
                    pass

        thread = threading.Thread(target=run, name="http-req", daemon=True)
        if getattr(self, "block_on_close", False):
            bucket = getattr(self, "_threads", None)
            if bucket is not None:
                bucket.append(thread)
        try:
            thread.start()
        except Exception:
            try:
                sema.release()
            except Exception:
                pass
            try:
                self.shutdown_request(request)
            except Exception:
                try:
                    request.close()
                except Exception:
                    pass
            raise


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


def on_fly() -> bool:
    """True only when this process is a Fly machine. Never inferred from client headers."""
    for name in ("FLY_APP_NAME", "FLY_ALLOC_ID", "FLY_MACHINE_ID"):
        if (os.environ.get(name) or "").strip():
            return True
    return False


def client_ip(handler: SimpleHTTPRequestHandler) -> str:
    """On Fly, trust Fly-Client-IP. Otherwise the socket peer. Never X-Forwarded-For."""
    if on_fly():
        fly = (handler.headers.get("Fly-Client-IP") or "").split(",")[0].strip()
        if fly:
            return fly
    if handler.client_address:
        return handler.client_address[0]
    return "unknown"


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
        """BaseHTTPRequestHandler owns the keep-alive loop. Cap is at process_request."""
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            self.close_connection = True
        if getattr(self, "close_connection", True):
            self._shutdown_client()

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

    def _coarse_endpoint(self) -> str:
        path = urlparse(self.path or "").path or "/"
        if path in {"/route", "/route/"}:
            return "route"
        if path == "/preview":
            return "preview"
        if path == "/validate":
            return "validate"
        if path == "/pulse":
            return "pulse"
        if path == "/rails":
            return "rails"
        if path == "/health":
            return "health"
        if path in {"/mcp", "/mcp.json"}:
            return "mcp"
        if path.startswith("/pq/log"):
            return "pq_log"
        if path == "/attestation":
            return "attestation"
        if path in HUMAN_PAGES or path in STATIC_FILES or path in HUMAN_DYNAMIC_PATHS:
            return "human"
        return "other"

    def _access_path(self) -> str:
        return urlparse(self.path or "").path or "/"

    def log_request(self, code="-", size="-"):
        self._write_access_log(code)

    def log_message(self, fmt: str, *args) -> None:
        # Do not emit the default request line (query string, headers, body).
        if getattr(self, "_logged_access", False):
            return
        self._write_access_log("-")

    def _write_access_log(self, code) -> None:
        self._logged_access = True
        started = getattr(self, "_req_started", None)
        latency = "-"
        if started is not None:
            try:
                latency = str(int(max(0.0, (time.monotonic() - started) * 1000)))
            except Exception:
                latency = "-"
        rid = getattr(self, "_request_id", None) or reqctx.request_id.get()
        line = "request_id=%s method=%s path=%s status=%s latency_ms=%s endpoint=%s\n" % (
            rid or "-",
            getattr(self, "command", None) or "-",
            self._access_path(),
            code,
            latency,
            self._coarse_endpoint(),
        )
        sys.stderr.write(line)

    def handle_one_request(self) -> None:
        self._request_id = uuid.uuid4().hex[:16]
        self._req_started = time.monotonic()
        self._logged_access = False
        token = reqctx.request_id.set(self._request_id)
        try:
            super().handle_one_request()
        finally:
            reqctx.request_id.reset(token)

    def version_string(self) -> str:
        """Do not advertise CPython / BaseHTTP version."""
        return "402Signal"

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
        data = asset_version.stamp_html(body).encode("utf-8")
        headers = {"Cache-Control": asset_version.HTML_REVALIDATE}
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

    def _read_human_html(self) -> str | None:
        parsed = urlparse(self.path)
        if parsed.path in HUMAN_DYNAMIC_PATHS:
            return self._transparency_html()
        injected = self._homepage_html()
        if injected is not None:
            return injected
        page = HUMAN_PAGES.get(parsed.path)
        if not page:
            return None
        return (STATIC_DIR / page).read_text(encoding="utf-8")

    def _serve_static_asset(self) -> None:
        parsed = urlparse(self.path)
        path = _ASSET_PATHS.get(parsed.path)
        if path is None:
            return self._json(404, {"error": "not found"})
        try:
            data = path.read_bytes()
        except OSError:
            return self._json(404, {"error": "not found"})
        suffix = path.suffix.lstrip(".")
        ctype = {
            "css": "text/css; charset=utf-8",
            "js": "text/javascript; charset=utf-8",
        }.get(suffix, "application/octet-stream")
        requested = (parse_qs(parsed.query).get("v") or [""])[0]
        cache = (
            asset_version.ASSET_LONG_CACHE
            if requested and requested == asset_version.asset_version()
            else asset_version.HTML_REVALIDATE
        )
        return self._bytes(200, data, ctype, {"Cache-Control": cache})

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
        return _ROUTE_LIMITER.allow(ip, route_rpm())

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
        human = self._read_human_html()
        if human is not None:
            extra = (
                {"Cache-Control": "no-store"}
                if parsed.path in HUMAN_DYNAMIC_PATHS
                else None
            )
            self._omit_body = True
            try:
                return self._html(200, human, extra)
            finally:
                self._omit_body = False
        if parsed.path in STATIC_FILES:
            self._omit_body = True
            try:
                return self._serve_static_asset()
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
        marker = "<!--PQ_LATEST-->"
        if marker in html:
            return html.replace(marker, section, 1)
        fallback = "</main>"
        if fallback not in html:
            return html
        return html.replace(fallback, section + "    </main>", 1)

    def _transparency_html(self) -> str:
        from live402.pq import transparency as pq_view

        return pq_view.render_html()

    def do_GET(self) -> None:
        if self._deny_private_store():
            return
        parsed = urlparse(self.path)
        human = self._read_human_html()
        if human is not None:
            extra = (
                {"Cache-Control": "no-store"}
                if parsed.path in HUMAN_DYNAMIC_PATHS
                else None
            )
            return self._html(200, human, extra)
        if parsed.path in STATIC_FILES:
            return self._serve_static_asset()
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
        if parsed.path == "/ready":
            payload = ready.readiness()
            return self._json(200 if payload.get("ok") else 503, payload)
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
    """HTTP process boot: production PQ identity, then log signer.

    PRODUCTION fail-closed: unset/unknown network never becomes TestNet.
    Automatic MainNet anchoring stays off. No Algorand SK load.
    """
    from live402.pq import log_identity

    if log_identity.is_production_runtime():
        log_identity.require_production_boot()
    boot_optional_log_signer()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve 402Signal locally")
    parser.add_argument("--host", default=default_host())
    parser.add_argument("--port", type=int, default=default_port())
    args = parser.parse_args(argv)
    assert_safe_http_boot(args.host)
    boot_http_process()
    httpd = BoundedThreadingHTTPServer((args.host, args.port), Handler)
    catalog.start_refresher()
    from live402.pq import worker as pq_worker

    pq_worker.start_worker()
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
