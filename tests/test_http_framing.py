"""PR1 A: strict HTTP request framing. No external network."""

from __future__ import annotations

import json
import os
import socket
import threading
import unittest
from email.message import Message
from http.server import ThreadingHTTPServer

os.environ.setdefault("LIVE402_FIXTURE", "1")
os.environ.pop("LOCAL_FREE", None)

from live402 import http_body, server
from live402.http_body import BodyReadError
from live402.server import Handler, MAX_BODY


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _host, port = httpd.server_address
    return httpd, port


def _raw(port, request: bytes, timeout=2.0) -> bytes:
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        sock.sendall(request)
        sock.settimeout(timeout)
        chunks = []
        while True:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _status(raw: bytes) -> int:
    if not raw:
        return 0
    line = raw.split(b"\r\n", 1)[0]
    if not line.startswith(b"HTTP/"):
        return 0
    parts = line.split()
    if len(parts) < 2:
        return 0
    try:
        return int(parts[1])
    except ValueError:
        return 0


def _recv_http_message(sock: socket.socket, timeout: float = 2.0) -> bytes:
    """Read one HTTP response. Leftover body bytes must not be treated as a new request."""
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            return buf
        buf += chunk
    header, rest = buf.split(b"\r\n\r\n", 1)
    content_length = None
    for line in header.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            try:
                content_length = int(line.split(b":", 1)[1].strip())
            except ValueError:
                content_length = None
            break
    if content_length is None:
        return buf
    while len(rest) < content_length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        rest += chunk
    return header + b"\r\n\r\n" + rest[:content_length]


def _headers_obj(*pairs: tuple[str, str]) -> Message:
    msg = Message()
    for key, val in pairs:
        msg.add_header(key, val)
    return msg


class ContentLengthUnitTests(unittest.TestCase):
    def test_missing_content_length(self):
        with self.assertRaises(BodyReadError) as ctx:
            http_body.declared_content_length(_headers_obj())
        self.assertEqual(ctx.exception.status, 400)

    def test_nonnumeric_content_length(self):
        with self.assertRaises(BodyReadError):
            http_body.declared_content_length(_headers_obj(("Content-Length", "abc")))

    def test_negative_and_signed_content_length(self):
        with self.assertRaises(BodyReadError):
            http_body.declared_content_length(_headers_obj(("Content-Length", "-1")))
        with self.assertRaises(BodyReadError):
            http_body.declared_content_length(_headers_obj(("Content-Length", "+10")))

    def test_duplicate_identical_and_conflicting(self):
        ident = _headers_obj(("Content-Length", "4"), ("Content-Length", "4"))
        with self.assertRaises(BodyReadError):
            http_body.declared_content_length(ident)
        conflict = _headers_obj(("Content-Length", "4"), ("Content-Length", "9"))
        with self.assertRaises(BodyReadError):
            http_body.declared_content_length(conflict)

    def test_transfer_encoding_rejected(self):
        with self.assertRaises(BodyReadError):
            http_body.declared_content_length(
                _headers_obj(("Transfer-Encoding", "chunked"), ("Content-Length", "2"))
            )
        with self.assertRaises(BodyReadError):
            http_body.declared_content_length(_headers_obj(("Transfer-Encoding", "chunked")))

    def test_get_all_not_get_only(self):
        class OnlyGet:
            def get(self, name, default=None):
                return "8"

            def get_all(self, name, default=None):
                return ["4", "9"]

        with self.assertRaises(BodyReadError):
            http_body.declared_content_length(OnlyGet())

    def test_never_read_negative(self):
        with self.assertRaises(BodyReadError):
            http_body.read_exactly(None, -1)

    def test_nan_rejected(self):
        with self.assertRaises(BodyReadError):
            http_body.loads_json_object(b'{"max_price_usd": NaN}')
        with self.assertRaises(BodyReadError):
            http_body.loads_json_object(b'{"max_price_usd": Infinity}')


class FramingServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["LOCAL_FREE"] = "1"
        os.environ["LIVE402_FIXTURE"] = "1"
        cls.httpd, cls.port = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        os.environ.pop("LOCAL_FREE", None)

    def _post(self, extra_headers: str, body: bytes, path="/route") -> bytes:
        req = (
            "POST %s HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n%s\r\n"
            % (path, extra_headers)
        ).encode("ascii") + body
        return _raw(self.port, req)

    def test_missing_cl(self):
        raw = self._post("", b'{"need":"weather"}')
        self.assertEqual(_status(raw), 400)

    def test_nonnumeric_cl(self):
        raw = self._post("Content-Length: nope\r\n", b"{}")
        self.assertEqual(_status(raw), 400)

    def test_negative_cl(self):
        raw = self._post("Content-Length: -1\r\n", b"{}")
        self.assertEqual(_status(raw), 400)

    def test_signed_plus_cl(self):
        raw = self._post("Content-Length: +10\r\n", b'{"need":"x"}')
        self.assertEqual(_status(raw), 400)

    def test_duplicate_identical_cl(self):
        body = b"{}"
        raw = self._post(
            "Content-Length: %d\r\nContent-Length: %d\r\n" % (len(body), len(body)),
            body,
        )
        self.assertEqual(_status(raw), 400)

    def test_duplicate_conflicting_cl(self):
        raw = self._post("Content-Length: 2\r\nContent-Length: 99\r\n", b"{}")
        self.assertEqual(_status(raw), 400)

    def test_te_chunked(self):
        raw = self._post("Transfer-Encoding: chunked\r\n", b"2\r\n{}\r\n0\r\n\r\n")
        self.assertEqual(_status(raw), 400)

    def test_te_plus_cl(self):
        raw = self._post("Transfer-Encoding: chunked\r\nContent-Length: 2\r\n", b"{}")
        self.assertEqual(_status(raw), 400)

    def test_zero_length_body(self):
        raw = self._post("Content-Length: 0\r\n", b"")
        self.assertIn(_status(raw), (400, 402))

    def test_exact_max_and_oversize(self):
        body = b"{" + b" " * (MAX_BODY - 2) + b"}"
        raw = self._post("Content-Length: %d\r\n" % len(body), body)
        self.assertNotEqual(_status(raw), 413)
        over = b"x" * (MAX_BODY + 1)
        raw = self._post("Content-Length: %d\r\n" % len(over), over)
        self.assertEqual(_status(raw), 413)
        self.assertIn(b"Connection: close", raw)

    def test_short_body(self):
        raw = _raw(
            self.port,
            (
                b"POST /route HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\nContent-Length: 40\r\n\r\n"
                b'{"need":"x"}'
            ),
            timeout=12,
        )
        self.assertEqual(_status(raw), 400)
        self.assertIn(b"Connection: close", raw)

    def test_malformed_json(self):
        body = b"{not-json"
        raw = self._post("Content-Length: %d\r\n" % len(body), body)
        self.assertEqual(_status(raw), 400)

    def test_framing_abuse_closes_persistent_connection(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        try:
            sock.sendall(
                b"POST /route HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Length: abc\r\n\r\n{}"
            )
            first = _recv_http_message(sock)
            self.assertEqual(_status(first), 400)
            self.assertIn(b"Connection: close", first)
            try:
                sock.sendall(
                    b"POST /route HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    b"Content-Length: 2\r\n\r\n{}"
                )
                second = sock.recv(4096)
            except (socket.timeout, ConnectionError, OSError):
                second = b""
            # Leftover first-response bytes are not a new status line. A
            # closed or poisoned keep-alive must never look like success.
            self.assertNotEqual(_status(second), 200)
            self.assertTrue(second == b"" or _status(second) in (0, 400))
        finally:
            sock.close()

    def test_mcp_and_validate_use_same_reader(self):
        for path in ("/mcp", "/validate"):
            raw = self._post("Transfer-Encoding: chunked\r\n", b"0\r\n\r\n", path=path)
            self.assertEqual(_status(raw), 400, path)

    def test_handler_saturation_fails_cleanly(self):
        prev = os.environ.get("LIVE402_MAX_HANDLERS")
        os.environ["LIVE402_MAX_HANDLERS"] = "1"
        server._HANDLER_SEMA = None
        server._HANDLER_SEMA_CAP = 0
        sema = server._handler_sema()
        self.assertTrue(sema.acquire(blocking=False))
        try:
            raw = self._post("Content-Length: 2\r\n", b"{}")
            self.assertEqual(_status(raw), 503)
            self.assertIn(b"server busy", raw)
        finally:
            sema.release()
            if prev is None:
                os.environ.pop("LIVE402_MAX_HANDLERS", None)
            else:
                os.environ["LIVE402_MAX_HANDLERS"] = prev
            server._HANDLER_SEMA = None
            server._HANDLER_SEMA_CAP = 0


class RateLimitCloseTests(unittest.TestCase):
    def test_rate_limited_post_closes_without_unbounded_discard(self):
        src = open(server.__file__, encoding="utf-8").read()
        self.assertIn("_close_unread_body", src)
        self.assertNotIn("self.rfile.read(-1)", src)
        self.assertNotIn("rfile.read(-1)", src)


if __name__ == "__main__":
    unittest.main()
