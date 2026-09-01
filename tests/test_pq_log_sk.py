"""Optional Ed25519 log signer from LIVE402_PQ_LOG_SK. Never a committed key."""

from __future__ import annotations

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

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from live402 import server
from live402.pq import events, receipt, store
from live402.route import handle_route


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


class LogSkEnvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        receipt.configure_signer(None)
        os.environ.pop("LIVE402_PQ_LOG_SK", None)
        os.environ.pop("LIVE402_PQ_LOG", None)
        os.environ.pop("LIVE402_PQ_LOG_VKEY", None)
        # Ephemeral test seed only. Never committed, never printed.
        self.seed = os.urandom(32)
        self.hex_sk = self.seed.hex()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        receipt.configure_signer(None)
        store.reset()
        os.environ.pop("LIVE402_PQ_LOG_SK", None)
        os.environ.pop("LIVE402_PQ_LOG", None)
        os.environ.pop("LIVE402_PQ_LOG_VKEY", None)
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        os.environ.pop("LOCAL_FREE", None)
        self.tmp.cleanup()

    def _assert_secret_absent(self, blob) -> None:
        if isinstance(blob, bytes):
            raw = blob
            text = blob.decode("utf-8", errors="replace")
        else:
            text = blob if isinstance(blob, str) else json.dumps(blob)
            raw = text.encode("utf-8")
        self.assertNotIn(self.hex_sk, text)
        self.assertNotIn(self.hex_sk.lower(), text.lower())
        self.assertNotIn(self.hex_sk.upper(), text)
        self.assertNotIn(self.seed, raw)
        self.assertNotIn(b"BEGIN PRIVATE KEY", raw)

    def test_unset_stays_unavailable_and_does_not_generate(self):
        os.environ.pop("LIVE402_PQ_LOG_SK", None)
        with patch.object(Ed25519PrivateKey, "generate", side_effect=AssertionError("must not generate")) as gen:
            vkey = receipt.load_signer_from_env()
            server.boot_optional_log_signer()
            gen.assert_not_called()
        self.assertEqual(vkey, "")
        self.assertIsNone(receipt.current_signer())
        self.assertFalse(receipt.available())
        out = receipt.attach_to_route(
            {"live": True, "url": "https://fixture.402signal.local/weather"},
            {"need": "weather"},
        )
        self.assertEqual(out["pq_trust"]["transparency"]["status"], "logged_uncheckpointed")
        self.assertNotEqual(out["pq_trust"]["transparency"]["status"], "pending")
        self.assertFalse(out["payment_authorization"]["pq_native"])

    def test_kill_switch_overrides_configured_signer(self):
        os.environ["LIVE402_PQ_LOG_SK"] = self.hex_sk
        os.environ["LIVE402_PQ_LOG"] = "0"
        vkey = receipt.load_signer_from_env()
        self.assertTrue(vkey)
        self.assertIsNotNone(receipt.current_signer())
        self.assertFalse(receipt.available())
        out = receipt.attach_to_route(
            {"live": True, "url": "https://fixture.402signal.local/weather"},
            {"need": "weather"},
        )
        self.assertEqual(out["pq_trust"]["transparency"]["status"], "unavailable")
        self.assertNotEqual(out["pq_trust"]["transparency"]["status"], "pending")
        self.assertNotIn("receipt", out["pq_trust"]["transparency"])
        self._assert_secret_absent(out)

    def test_hex_seed_loads_and_issue_is_verifiable(self):
        os.environ["LIVE402_PQ_LOG_SK"] = self.hex_sk
        vkey = receipt.load_signer_from_env()
        self.assertTrue(vkey)
        self.assertEqual(os.environ.get("LIVE402_PQ_LOG_VKEY"), vkey)
        self.assertEqual(store.meta_get("vkey"), vkey)
        self.assertTrue(receipt.available())
        ev = events.route_decision_event(need="secret-need", ts=1756627200)
        proof = receipt.issue(ev)
        receipt.verify_receipt(proof, vkey)
        self.assertIn("checkpoint", proof)
        self.assertGreaterEqual(proof["index"], 0)
        self._assert_secret_absent(proof)
        self._assert_secret_absent(store.meta_get("vkey"))
        self._assert_secret_absent(store.latest_checkpoint())

    def test_hex_with_0x_and_whitespace_loads(self):
        os.environ["LIVE402_PQ_LOG_SK"] = "  0x" + self.hex_sk + "\n"
        vkey = receipt.load_signer_from_env()
        self.assertTrue(vkey)
        proof = receipt.issue(events.route_decision_event(need="x", ts=1756627201))
        receipt.verify_receipt(proof, vkey)

    def test_pkcs8_pem_loads(self):
        key = Ed25519PrivateKey.from_private_bytes(self.seed)
        pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode("ascii")
        os.environ["LIVE402_PQ_LOG_SK"] = pem
        vkey = receipt.load_signer_from_env()
        self.assertTrue(vkey)
        proof = receipt.issue(events.route_decision_event(need="x", ts=1756627202))
        receipt.verify_receipt(proof, vkey)
        self._assert_secret_absent(proof)
        self.assertNotIn("BEGIN PRIVATE KEY", json.dumps(proof))
        self.assertNotIn("".join(pem.split()), json.dumps(proof).replace("\n", ""))

    def test_malformed_secret_does_not_configure_signer(self):
        prior = Ed25519PrivateKey.from_private_bytes(self.seed)
        receipt.configure_signer(prior)
        self.assertIsNotNone(receipt.current_signer())
        os.environ["LIVE402_PQ_LOG_SK"] = "not-a-key"
        buf = StringIO()
        with patch.object(Ed25519PrivateKey, "generate", side_effect=AssertionError("must not generate")) as gen:
            with patch("sys.stderr", buf):
                vkey = receipt.load_signer_from_env()
            gen.assert_not_called()
        self.assertEqual(vkey, "")
        self.assertIsNone(receipt.current_signer())
        self.assertFalse(receipt.available())
        err = buf.getvalue()
        self.assertIn("malformed", err)
        self.assertNotIn("not-a-key", err)
        self.assertNotIn(self.hex_sk, err)
        out = receipt.attach_to_route(
            {"live": True, "url": "https://fixture.402signal.local/weather"},
            {"need": "weather"},
        )
        self.assertEqual(out["pq_trust"]["transparency"]["status"], "logged_uncheckpointed")
        self.assertNotEqual(out["pq_trust"]["transparency"]["status"], "pending")

    def test_malformed_hex_length_and_pem_fail_closed(self):
        for bad in ("ab", "zz" * 32, self.hex_sk + "00", "-----BEGIN PRIVATE KEY-----\nbad\n-----END PRIVATE KEY-----"):
            os.environ["LIVE402_PQ_LOG_SK"] = bad
            with patch("sys.stderr", StringIO()):
                self.assertEqual(receipt.load_signer_from_env(), "")
            self.assertIsNone(receipt.current_signer())
            self.assertFalse(receipt.available())

    def test_responses_and_sqlite_never_contain_secret(self):
        os.environ["LIVE402_PQ_LOG_SK"] = self.hex_sk
        os.environ["LOCAL_FREE"] = "1"
        receipt.load_signer_from_env()
        code, body, _extra = handle_route(
            {"need": "weather", "url": "https://fixture.402signal.local/weather"},
            {},
            "https://402signal.com/route",
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["pq_trust"]["transparency"]["status"], "pending")
        self._assert_secret_absent(body)
        self._assert_secret_absent(json.dumps(body))

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
            for table, col in (("meta", "v"), ("checkpoints", "note")):
                for row in conn.execute("SELECT %s FROM %s" % (col, table)):
                    self._assert_secret_absent(row[0] if row[0] is not None else "")
        finally:
            conn.close()

    def test_main_calls_boot_hook(self):
        import inspect

        src = inspect.getsource(server.main)
        self.assertIn("boot_http_process", src)
        self.assertNotIn("boot_optional_falcon_sk", src)
        boot = inspect.getsource(server.boot_http_process)
        self.assertIn("boot_optional_log_signer", boot)
        self.assertNotIn("boot_optional_falcon_sk", boot)
        self.assertNotIn("load_falcon_sk_from_env", boot)


if __name__ == "__main__":
    unittest.main()
