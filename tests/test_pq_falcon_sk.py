"""Falcon SK must stay off 402signal. Router has no LIVE402_PQ_FALCON_SK path."""

from __future__ import annotations

import inspect
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import server
from live402.pq import algo_anchor, isolated_signer, signer_client, store, worker


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


class FalconSkAbsentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        worker.clear_queue()
        os.environ.pop("LIVE402_PQ_FALCON_SK", None)
        os.environ.pop("LIVE402_PQ_SIGNER_TOKEN", None)
        os.environ.pop("FLY_PROCESS_GROUP", None)
        os.environ.pop("LOCAL_FREE", None)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        worker.clear_queue()
        store.reset()
        os.environ.pop("LIVE402_PQ_FALCON_SK", None)
        os.environ.pop("LIVE402_PQ_SIGNER_TOKEN", None)
        os.environ.pop("FLY_PROCESS_GROUP", None)
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        os.environ.pop("LOCAL_FREE", None)
        self.tmp.cleanup()

    def test_router_modules_have_no_falcon_sk_loader(self):
        self.assertFalse(hasattr(algo_anchor, "load_falcon_sk_from_env"))
        self.assertFalse(hasattr(algo_anchor, "configure_falcon_sk"))
        self.assertFalse(hasattr(algo_anchor, "current_falcon_sk"))
        self.assertFalse(hasattr(server, "boot_optional_falcon_sk"))
        self.assertFalse(hasattr(isolated_signer, "boot"))
        self.assertFalse(hasattr(isolated_signer, "bind_ipc"))

    def test_secret_name_absent_from_shipped_files(self):
        root = Path(__file__).resolve().parent.parent
        paths = [
            root / "fly.toml",
            root / "Dockerfile",
            root / "live402" / "pq" / "algo_anchor.py",
            root / "live402" / "pq" / "isolated_signer.py",
            root / "live402" / "pq" / "signer_client.py",
            root / "live402" / "pq" / "worker.py",
            root / "live402" / "pq" / "trust_root.v1.json",
            root / "live402" / "server.py",
        ]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("LIVE402_PQ_FALCON_SK", text, path.name)

    def test_http_boot_does_not_dial_or_load_sk(self):
        src = inspect.getsource(server.main)
        self.assertNotIn("boot_optional_falcon_sk", src)
        self.assertNotIn("load_falcon_sk_from_env", src)
        os.environ["LIVE402_PQ_FALCON_SK"] = "should-be-ignored"
        with patch("socket.create_connection", side_effect=AssertionError("must not dial")) as dial:
            server.boot_http_process()
            dial.assert_not_called()

    def test_responses_do_not_mention_sk_env(self):
        os.environ["LOCAL_FREE"] = "1"
        httpd, port = _serve()
        try:
            for path in ("/route", "/pq/log/checkpoint", "/health"):
                conn = HTTPConnection("127.0.0.1", port, timeout=5)
                if path == "/route":
                    conn.request(
                        "POST",
                        "/route",
                        body=b'{"need":"weather","url":"https://fixture.402signal.local/weather"}',
                        headers={"Content-Type": "application/json"},
                    )
                else:
                    conn.request("GET", path)
                res = conn.getresponse()
                raw = res.read()
                conn.close()
                self.assertNotIn(b"LIVE402_PQ_FALCON_SK", raw)
                self.assertNotIn(b"BEGIN PRIVATE KEY", raw)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_token_unset_is_c1_noop(self):
        store.append(b"one")
        worker.save_anchor(0, 0)
        with patch.object(os, "urandom", wraps=os.urandom):
            with patch("socket.create_connection", side_effect=AssertionError("must not dial")) as dial:
                out = worker.maybe_submit(lambda _u: b"sig", now=15 * 60, send_fn=lambda _b: "txid")
                dial.assert_not_called()
        self.assertIsNone(out)
        self.assertFalse(signer_client.token_configured())


if __name__ == "__main__":
    unittest.main()
