"""Outbound http.client gh-150743 regression. No live seller."""

from __future__ import annotations

import http.client
import inspect
import os
import socket
import threading
import unittest
from pathlib import Path

os.environ.setdefault("LIVE402_FIXTURE", "1")

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKER_MD = (ROOT / "docs" / "docker.md").read_text(encoding="utf-8")

PINNED = "python:3.12.14-slim@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc"


def _runtime_has_gh150743() -> bool:
    try:
        params = inspect.signature(http.client.HTTPConnection.__init__).parameters
    except (TypeError, ValueError):
        params = {}
    if "max_response_headers" in params:
        return True
    try:
        src = inspect.getsource(http.client.HTTPResponse)
    except OSError:
        return False
    return "interim" in src.lower() and "trailer" in src.lower()


class OutboundHttpClientPinTests(unittest.TestCase):
    def test_dockerfile_is_31214_with_official_digest(self):
        self.assertIn(PINNED, DOCKERFILE)
        self.assertIn("gh-150743", DOCKERFILE)
        self.assertNotIn("3.12.11-slim@", DOCKERFILE)
        self.assertIn(PINNED, DOCKER_MD)
        self.assertIn("gh-150743", DOCKER_MD)
        self.assertIn("does **not** contain that fix", DOCKER_MD)
        self.assertIn("3.12.11", DOCKER_MD)

    def test_probe_requests_max_response_headers(self):
        from live402 import probe

        src = inspect.getsource(probe._PinnedHTTPSConnection.__init__)
        self.assertIn("max_response_headers", src)
        self.assertIn("100", src)


@unittest.skipUnless(_runtime_has_gh150743(), "runtime lacks gh-150743; Docker pin is 3.12.14")
class OutboundHttpClientFixTests(unittest.TestCase):
    def _serve(self, chunks):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        stop = threading.Event()

        def run():
            sock.listen(1)
            sock.settimeout(2)
            try:
                conn, _addr = sock.accept()
            except OSError:
                return
            try:
                conn.recv(4096)
                for chunk in chunks:
                    conn.sendall(chunk)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
                stop.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(sock.close)
        return port

    def test_interim_1xx_flood_raises(self):
        flood = b"".join(b"HTTP/1.1 100 Continue\r\n\r\n" for _ in range(120))
        port = self._serve([flood])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("GET", "/")
            with self.assertRaises(http.client.HTTPException):
                conn.getresponse()
        finally:
            conn.close()

    def test_chunked_trailer_flood_raises(self):
        # gh-150743 reads chunked trailers in HTTPResponse.read() after the
        # last-chunk size 0, not during getresponse() header parsing.
        head = (
            b"HTTP/1.1 200 OK\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"0\r\n"
        )
        trailers = b"".join(b"X-T-%d: 1\r\n" % i for i in range(120))
        port = self._serve([head + trailers])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("GET", "/")
            try:
                resp = conn.getresponse()
            except http.client.HTTPException:
                return
            with self.assertRaises(http.client.HTTPException):
                resp.read()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
