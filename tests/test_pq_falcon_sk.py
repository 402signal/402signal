"""Optional Falcon-1024 SK from LIVE402_PQ_FALCON_SK. Never a committed key."""

from __future__ import annotations

import base64
import inspect
import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from io import StringIO
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import payment, server
from live402.pq import algo_anchor, store, worker


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


class FalconSkEnvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        worker.clear_queue()
        algo_anchor.configure_falcon_sk(None)
        os.environ.pop("LIVE402_PQ_FALCON_SK", None)
        os.environ.pop("LIVE402_PQ_FALCON_NETWORK", None)
        os.environ.pop("LIVE402_PQ_FALCON_BROADCAST", None)
        os.environ.pop("LIVE402_PQ_FALCON_ADDRESS", None)
        os.environ.pop("FLY_PROCESS_GROUP", None)
        # Ephemeral test SK only. Never committed, never printed.
        self.sk = os.urandom(algo_anchor.FALCON_SK_LEN)
        self.hex_sk = self.sk.hex()
        self.b64_sk = base64.b64encode(self.sk).decode("ascii")
        self.sender = payment.DEFAULT_PAYTO_ALGORAND
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        algo_anchor.configure_falcon_sk(None)
        worker.clear_queue()
        store.reset()
        os.environ.pop("LIVE402_PQ_FALCON_SK", None)
        os.environ.pop("LIVE402_PQ_FALCON_NETWORK", None)
        os.environ.pop("LIVE402_PQ_FALCON_BROADCAST", None)
        os.environ.pop("LIVE402_PQ_FALCON_ADDRESS", None)
        os.environ.pop("FLY_PROCESS_GROUP", None)
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        os.environ.pop("LOCAL_FREE", None)
        self.tmp.cleanup()

    def _assert_secret_absent(self, blob) -> None:
        if isinstance(blob, bytes):
            raw = blob
            text = blob.decode("utf-8", errors="replace")
        else:
            text = blob if isinstance(blob, str) else json.dumps(blob, default=str)
            raw = text.encode("utf-8")
        self.assertNotIn(self.hex_sk, text)
        self.assertNotIn(self.hex_sk.lower(), text.lower())
        self.assertNotIn(self.hex_sk.upper(), text)
        self.assertNotIn(self.b64_sk, text)
        self.assertNotIn(self.sk, raw)
        self.assertNotIn(b"BEGIN PRIVATE KEY", raw)

    def _walk(self, obj):
        if isinstance(obj, dict):
            for key, val in obj.items():
                yield key
                yield from self._walk(val)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                yield from self._walk(item)
        else:
            yield obj

    def test_unset_is_construction_only_and_does_not_generate(self):
        os.environ.pop("LIVE402_PQ_FALCON_SK", None)
        with patch.object(os, "urandom", side_effect=AssertionError("must not generate")):
            loaded = algo_anchor.load_falcon_sk_from_env()
            server.boot_optional_falcon_sk()
        self.assertFalse(loaded)
        self.assertIsNone(algo_anchor.current_falcon_sk())
        store.append(b"one")
        worker.save_anchor(0, 0)
        worker.enqueue_unsigned(now=15 * 60)
        out = worker.process_one(lambda _u: b"sig", self.sender, now=15 * 60)
        self.assertIsNotNone(out)
        self.assertFalse(out["submitted"])

    def test_hex_sk_loads_and_never_appears_in_submit_result(self):
        os.environ["LIVE402_PQ_FALCON_SK"] = self.hex_sk
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = self.sender
        self.assertTrue(algo_anchor.load_falcon_sk_from_env())
        self.assertEqual(len(algo_anchor.current_falcon_sk() or b""), algo_anchor.FALCON_SK_LEN)
        store.append(b"one")
        worker.save_anchor(0, 0)
        out = worker.maybe_submit(
            lambda _u: b"pqsig-no-sk",
            self.sender,
            {
                "genesisID": algo_anchor.TESTNET_GENESIS_ID,
                "genesisHash": algo_anchor.TESTNET_GENESIS_HASH,
                "firstValid": 1,
                "lastValid": 1001,
                "fee": 3000,
            },
            now=15 * 60,
            send_fn=lambda _blob: "txid-mock",
        )
        self.assertIsNotNone(out)
        self.assertTrue(out["submitted"])
        self.assertEqual(out["pqsig"], b"pqsig-no-sk")
        self.assertNotEqual(out["pqsig"], self.sk)
        self._assert_secret_absent(out)
        for item in self._walk(out):
            if isinstance(item, (bytes, bytearray)):
                self.assertNotEqual(bytes(item), self.sk)
            if isinstance(item, str):
                self.assertNotIn(self.hex_sk, item)

    def test_hex_with_0x_and_whitespace_loads(self):
        os.environ["LIVE402_PQ_FALCON_SK"] = "  0x" + self.hex_sk + "\n"
        self.assertTrue(algo_anchor.load_falcon_sk_from_env())
        self.assertEqual(algo_anchor.current_falcon_sk(), self.sk)

    def test_base64_sk_loads(self):
        os.environ["LIVE402_PQ_FALCON_SK"] = self.b64_sk
        self.assertTrue(algo_anchor.load_falcon_sk_from_env())
        self.assertEqual(algo_anchor.current_falcon_sk(), self.sk)

    def test_malformed_secret_fails_closed_no_autogen(self):
        algo_anchor.configure_falcon_sk(self.sk)
        self.assertIsNotNone(algo_anchor.current_falcon_sk())
        os.environ["LIVE402_PQ_FALCON_SK"] = "not-a-key"
        buf = StringIO()
        with patch.object(os, "urandom", side_effect=AssertionError("must not generate")) as gen:
            with patch("sys.stderr", buf):
                loaded = algo_anchor.load_falcon_sk_from_env()
            gen.assert_not_called()
        self.assertFalse(loaded)
        self.assertIsNone(algo_anchor.current_falcon_sk())
        err = buf.getvalue()
        self.assertIn("malformed", err)
        self.assertNotIn("not-a-key", err)
        self.assertNotIn(self.hex_sk, err)

    def test_ed25519_log_hex_is_rejected(self):
        os.environ["LIVE402_PQ_FALCON_SK"] = os.urandom(32).hex()
        with patch("sys.stderr", StringIO()):
            self.assertFalse(algo_anchor.load_falcon_sk_from_env())
        self.assertIsNone(algo_anchor.current_falcon_sk())

    def test_malformed_lengths_fail_closed(self):
        for bad in ("ab", "zz" * 32, self.hex_sk + "00", "-----BEGIN PRIVATE KEY-----\nbad\n-----END PRIVATE KEY-----"):
            os.environ["LIVE402_PQ_FALCON_SK"] = bad
            with patch("sys.stderr", StringIO()):
                self.assertFalse(algo_anchor.load_falcon_sk_from_env())
            self.assertIsNone(algo_anchor.current_falcon_sk())

    def test_responses_and_logs_never_contain_secret(self):
        os.environ["LIVE402_PQ_FALCON_SK"] = self.hex_sk
        os.environ["LOCAL_FREE"] = "1"
        algo_anchor.load_falcon_sk_from_env()
        server.boot_optional_falcon_sk()
        httpd, port = _serve()
        try:
            for path in ("/route", "/pq/log/checkpoint", "/health", "/data", "/data/pq-log.sqlite"):
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
                self._assert_secret_absent(raw)
                if path in ("/data", "/data/pq-log.sqlite"):
                    self.assertEqual(res.status, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()

        import sqlite3

        conn = sqlite3.connect(os.environ["LIVE402_PQ_LOG_DB"])
        try:
            for table in ("meta", "checkpoints"):
                for row in conn.execute("SELECT * FROM %s" % table):
                    for cell in row:
                        self._assert_secret_absent(cell if cell is not None else "")
        finally:
            conn.close()

    def test_http_app_does_not_load_falcon_sk(self):
        src = inspect.getsource(server.main)
        self.assertNotIn("boot_optional_falcon_sk", src)
        self.assertNotIn("load_falcon_sk_from_env", src)
        self.assertIn("boot_http_process", src)

        os.environ["LIVE402_PQ_FALCON_SK"] = self.hex_sk
        os.environ["FLY_PROCESS_GROUP"] = "app"
        algo_anchor.configure_falcon_sk(None)
        server.boot_http_process()
        self.assertIsNone(algo_anchor.current_falcon_sk())

        from live402.pq import isolated_signer

        iso_src = inspect.getsource(isolated_signer.main)
        self.assertIn("boot", iso_src)
        self.assertNotIn("load_signer_from_env", iso_src)
        self.assertNotIn("boot_optional_log_signer", iso_src)
        self.assertNotIn("ThreadingHTTPServer", iso_src)
        self.assertNotIn("/route", iso_src)
        self.assertIn("FLY_PROCESS_GROUP", inspect.getsource(isolated_signer))
        falcon_boot = inspect.getsource(isolated_signer.boot)
        self.assertIn("load_falcon_sk_from_env", falcon_boot)
        self.assertNotIn("load_signer_from_env", falcon_boot)


if __name__ == "__main__":
    unittest.main()
