"""C2SP HTTP read API and /data deny."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

os.environ.setdefault("LIVE402_FIXTURE", "1")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from live402 import discover
from live402.pq import events, receipt, store
from live402.server import Handler, is_private_store_path
from pathlib import Path


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


class C2SPHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(cls.tmp.name, "pq-log.sqlite")
        store.reset()
        cls.key = Ed25519PrivateKey.generate()
        receipt.configure_signer(cls.key)
        receipt.issue(events.route_decision_event(need="hidden", ts=1756627200))
        receipt.issue(events.observation_batch_event(batch_id="b1", n=1, digest="a" * 64, ts=1756627201))
        cls.httpd, cls.port = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        receipt.configure_signer(None)
        store.reset()
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        cls.tmp.cleanup()

    def _get(self, path, method="GET"):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path)
        res = conn.getresponse()
        raw = res.read()
        hdrs = {k.lower(): v for k, v in res.getheaders()}
        conn.close()
        return res.status, raw, hdrs

    def test_checkpoint_is_text_plain(self):
        status, raw, hdrs = self._get("/pq/log/checkpoint")
        self.assertEqual(status, 200)
        self.assertTrue(hdrs.get("content-type", "").startswith("text/plain"))
        self.assertIn("charset=utf-8", hdrs.get("content-type", ""))
        text = raw.decode("utf-8")
        self.assertTrue(text.startswith("402signal.com/pq/log\n"))
        self.assertGreaterEqual(int(text.split("\n")[1]), 1)

    def test_tiles_and_entry_bundles(self):
        status, raw, hdrs = self._get("/pq/log/tile/0/0.p/2")
        self.assertEqual(status, 200, raw)
        self.assertEqual(hdrs.get("content-type"), "application/octet-stream")
        self.assertEqual(len(raw), 64)
        status, bundle, hdrs = self._get("/pq/log/tile/entries/0.p/2")
        self.assertEqual(status, 200)
        self.assertEqual(hdrs.get("content-type"), "application/octet-stream")
        self.assertGreater(len(bundle), 4)
        self.assertNotIn(b"hidden", bundle)
        status, _raw, _hdrs = self._get("/pq/log/tile/8/0/0")
        self.assertEqual(status, 404)

    def test_checkpoint_at_tree_size(self):
        latest_status, latest, _hdrs = self._get("/pq/log/checkpoint")
        self.assertEqual(latest_status, 200)
        size = int(latest.decode("utf-8").split("\n")[1])
        status, raw, hdrs = self._get("/pq/log/checkpoint/%s" % size)
        self.assertEqual(status, 200)
        self.assertTrue(hdrs.get("content-type", "").startswith("text/plain"))
        self.assertEqual(raw, latest)
        alias_status, alias, alias_hdrs = self._get("/pq/log/checkpoint/latest")
        self.assertEqual(alias_status, 200)
        self.assertTrue(alias_hdrs.get("content-type", "").startswith("text/plain"))
        self.assertEqual(alias, latest)
        status, _raw, _hdrs = self._get("/pq/log/checkpoint/0")
        self.assertEqual(status, 404)
        status, _raw, _hdrs = self._get("/pq/log/checkpoint/01")
        self.assertEqual(status, 404)
        status, _raw, _hdrs = self._get("/pq/log/checkpoint/999999")
        self.assertEqual(status, 404)

    def test_checkpoint_size_rejects_junk_without_500(self):
        cases = (
            "/pq/log/checkpoint/" + ("9" * 500),
            "/pq/log/checkpoint/-1",
            "/pq/log/checkpoint/+1",
            "/pq/log/checkpoint/01",
            "/pq/log/checkpoint/0",
            "/pq/log/checkpoint/1e2",
            "/pq/log/checkpoint/1%201",
            "/pq/log/checkpoint/%201",
            "/pq/log/checkpoint/9223372036854775808",
            "/pq/log/checkpoint/junk",
            "/pq/log/checkpoint/1/",
        )
        for path in cases:
            status, raw, hdrs = self._get(path)
            self.assertEqual(status, 404, path)
            self.assertNotEqual(status, 500, path)
            self.assertTrue(raw, path)
            self.assertIn("json", (hdrs.get("content-type") or "").lower(), path)

    def test_docs_falcon_anchoring_wording(self):
        spec = json.dumps(discover.openapi_spec())
        docs = spec + "\n" + discover.GUIDANCE + "\n" + discover.LLMS_TXT
        self.assertIn("Production log identity targets Algorand MainNet", spec)
        self.assertIn("Awaiting first confirmed MainNet checkpoint", spec)
        self.assertIn("MainNet Falcon broadcasting is not enabled", spec)
        self.assertIn("Production transparency log identity targets Algorand MainNet", discover.GUIDANCE)
        self.assertIn("Production transparency log identity targets Algorand MainNet", discover.LLMS_TXT)
        self.assertIn("Signer never reads BROADCAST and never POSTs", docs)
        self.assertIn("/route does not wait for chain", docs)
        self.assertIn("Falcon authorizes a checkpoint txn, not a merchant payment", docs)
        self.assertNotIn("off by default", docs)
        self.assertNotIn("must GO before LIVE402_PQ_FALCON_BROADCAST=1", docs)
        self.assertNotIn("default unset", docs)

    def test_head_checkpoint(self):
        status, raw, hdrs = self._get("/pq/log/checkpoint", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"")
        self.assertGreater(int(hdrs.get("content-length") or 0), 0)

    def test_data_and_sqlite_still_404(self):
        for path in (
            "/data",
            "/data/",
            "/data/pq-log.sqlite",
            "/data/pq-log-mainnet.sqlite",
            "/data/catalog.sqlite",
            "/data/live402-history.sqlite",
            "/pq-log.sqlite",
            "/pq-log-mainnet.sqlite",
            "/catalog.sqlite",
        ):
            status, raw, hdrs = self._get(path)
            self.assertEqual(status, 404, path)
            self.assertFalse(raw.startswith(b"SQLite format 3"), path)
            self.assertIn("json", (hdrs.get("content-type") or "").lower())
            body = json.loads(raw.decode("utf-8"))
            self.assertEqual(body.get("error"), "not found")
            self.assertTrue(is_private_store_path(path) or path in {"/data", "/data/"})

    def test_no_trust_page(self):
        status, raw, _hdrs = self._get("/trust")
        self.assertEqual(status, 404)
        status, raw, _hdrs = self._get("/pq/log")
        self.assertEqual(status, 404)

    def test_docs_say_experimental_mainnet_defaults(self):
        spec = json.dumps(discover.openapi_spec())
        self.assertIn("/pq/log/checkpoint", spec)
        self.assertIn("experimental", spec.lower())
        self.assertIn("Production log identity targets Algorand MainNet", spec)
        self.assertIn("MainNet Falcon broadcasting is not enabled", spec)
        self.assertIn("Awaiting first confirmed MainNet checkpoint", spec)
        self.assertNotIn("Falcon anchoring is TestNet-only", spec)
        self.assertNotIn("pq_secure", spec)
        self.assertIn("GET /pq/log/checkpoint", discover.LLMS_TXT)
        self.assertIn("Production transparency log identity targets Algorand MainNet", discover.LLMS_TXT)
        self.assertIn("MainNet Falcon broadcasting is not enabled", discover.LLMS_TXT)
        self.assertIn("Awaiting first confirmed MainNet checkpoint", discover.LLMS_TXT)
        home = (Path(__file__).resolve().parent.parent / "live402" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("Algorand MainNet", home)
        self.assertIn("Awaiting anchor", home)
        self.assertIn("402Signal commits routing evidence to an append-only log.", home)
        self.assertNotIn("/pq/log", home)


if __name__ == "__main__":
    unittest.main()
