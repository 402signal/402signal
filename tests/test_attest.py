"""Public attestation hash of observed probe batches. Not on-chain."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

os.environ.setdefault("LIVE402_FIXTURE", "1")
os.environ.pop("LOCAL_FREE", None)

from live402.server import Handler
from live402 import history, probe, validate


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return httpd, host, port


def _get(port, path, extra_headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=dict(extra_headers or {}))
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        data = raw.decode("utf-8")
    return res.status, data


def _json_post(port, path, payload, extra_headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    conn.request("POST", path, body=body, headers=headers)
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        data = raw.decode("utf-8")
    return res.status, data


class CanonicalHashTests(unittest.TestCase):
    def test_hash_stable_across_row_order(self):
        rows_a = [
            {"url": "https://b.example/x", "field": "live", "value": "1", "ts": 20},
            {"url": "https://a.example/x", "field": "payTo", "value": "0xabc", "ts": 10},
        ]
        rows_b = list(reversed(rows_a))
        ca = history.canonical_observation_rows(rows_a)
        cb = history.canonical_observation_rows(rows_b)
        self.assertEqual(ca, cb)
        self.assertEqual(history.hash_canonical(ca), history.hash_canonical(cb))
        self.assertEqual(len(history.hash_canonical(ca)), 64)
        parsed = json.loads(ca)
        self.assertEqual(parsed[0]["url"], "https://a.example/x")
        blob = ca.lower()
        for banned in ("signature", "payment-signature", "x-payment", "envelope", "secret", "private", "key"):
            self.assertNotIn(banned, blob)

    def test_hash_changes_when_value_changes(self):
        a = history.canonical_observation_rows(
            [{"url": "https://a.example/x", "field": "live", "value": "1", "ts": 1}]
        )
        b = history.canonical_observation_rows(
            [{"url": "https://a.example/x", "field": "live", "value": "0", "ts": 1}]
        )
        self.assertNotEqual(history.hash_canonical(a), history.hash_canonical(b))


class AttestationHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("LOCAL_FREE", None)
        os.environ["LIVE402_FIXTURE"] = "1"
        fd, cls._db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        cls._prev_db = os.environ.get("LIVE402_HISTORY_DB")
        os.environ["LIVE402_HISTORY_DB"] = cls._db
        history.reset()
        cls.httpd, cls.host, cls.port = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        history.reset()
        if cls._prev_db is None:
            os.environ.pop("LIVE402_HISTORY_DB", None)
        else:
            os.environ["LIVE402_HISTORY_DB"] = cls._prev_db
        for pth in (cls._db, cls._db + "-wal", cls._db + "-shm"):
            try:
                os.remove(pth)
            except OSError:
                pass

    def test_no_batch_is_404_fail_closed(self):
        history.reset()
        status, body = _get(self.port, "/attestation")
        self.assertEqual(status, 404)
        self.assertEqual(body.get("error"), "no_batch")
        self.assertNotIn("hash", body)

    def test_validate_does_not_create_attestation_batch(self):
        history.reset()
        vstatus, vbody = _json_post(
            self.port,
            "/validate",
            {"url": "https://fixture.402signal.local/weather"},
        )
        self.assertEqual(vstatus, 200)
        status, body = _get(self.port, "/attestation")
        self.assertEqual(status, 404)
        self.assertEqual(body.get("error"), "no_batch")
        self.assertNotIn("hash", body)

    def test_attestation_after_recorded_probe(self):
        history.reset()
        probe.probe_url("https://fixture.402signal.local/weather", record=True)
        status, body = _get(self.port, "/attestation")
        self.assertEqual(status, 200)
        for key in ("batch_id", "created_at", "n", "algo", "hash"):
            self.assertIn(key, body)
        self.assertEqual(body["algo"], "sha256")
        self.assertEqual(len(body["hash"]), 64)
        self.assertGreater(int(body["n"]), 0)
        blob = json.dumps(body).lower()
        for banned in (
            "signature",
            "payment-signature",
            "x-payment",
            "envelope",
            "secret",
            "private_key",
            "api_key",
            "authorization",
            "header",
        ):
            self.assertNotIn(banned, blob)
        self.assertEqual(list(body.keys()), ["batch_id", "created_at", "n", "algo", "hash"])
        again = history.attestation_for(body["batch_id"])
        self.assertEqual(again["hash"], body["hash"])
        status2, body2 = _get(self.port, "/attestation?batch_id=" + body["batch_id"])
        self.assertEqual(status2, 200)
        self.assertEqual(body2["hash"], body["hash"])

    def test_unknown_batch_404(self):
        status, body = _get(self.port, "/attestation?batch_id=not-a-real-batch")
        self.assertEqual(status, 404)
        self.assertNotIn("hash", body)

    def test_openapi_and_llms_document_attestation(self):
        status, spec = _get(self.port, "/openapi.json")
        self.assertEqual(status, 200)
        self.assertIn("/attestation", spec["paths"])
        status, llms = _get(self.port, "/llms.txt")
        self.assertEqual(status, 200)
        self.assertIn("GET /attestation", llms)


class PulseObservedThinTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("LIVE402_HISTORY_DB")
        fd, self._path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.environ["LIVE402_HISTORY_DB"] = self._path
        history.reset()

    def tearDown(self):
        history.reset()
        if self._prev is None:
            os.environ.pop("LIVE402_HISTORY_DB", None)
        else:
            os.environ["LIVE402_HISTORY_DB"] = self._prev
        for pth in (self._path, self._path + "-wal", self._path + "-shm"):
            try:
                os.remove(pth)
            except OSError:
                pass

    def test_thin_window_omits_healthy_and_enr(self):
        url = "https://hist.example/thin"
        history.record_probe(url, {
            "live": True,
            "status": 402,
            "latency_ms": 10,
            "payTo": "0xabc",
            "batch_id": "thinbatch1",
        })
        obs = history.pulse_observed()
        self.assertEqual(obs.get("n_7d"), 1)
        self.assertEqual(obs.get("reliability"), "unknown")
        self.assertNotIn("healthy", obs)
        self.assertNotIn("executable_now_rate", obs)
        self.assertNotIn("success_7d", obs)

    def test_enough_samples_emits_rates_not_healthy(self):
        url = "https://hist.example/full"
        for i in range(10):
            history.record_probe(url, {
                "live": True,
                "status": 402,
                "latency_ms": 10,
                "payTo": "0xabc",
                "batch_id": "fullbatch",
                "schema_present": 1,
            })
        obs = history.pulse_observed()
        self.assertGreaterEqual(obs.get("n_7d"), 10)
        self.assertNotIn("healthy", obs)
        self.assertNotIn("executable_now_rate", obs)
        self.assertIn("success_7d", obs)
        self.assertGreater(obs.get("success_7d"), 0)
        self.assertIn("payable_rate_7d", obs)
        self.assertIn("invocable_rate_7d", obs)
        self.assertNotIn("reliability", obs)

    def test_pulse_collect_uses_observed_and_omits_invented_enr(self):
        from live402 import pulse as pulse_mod
        pulse_mod.reset_cache()
        payload = pulse_mod.get_pulse()
        self.assertTrue(payload.get("ok"))
        obs = payload.get("observed") or {}
        self.assertEqual(obs.get("source"), "402signal_observed")
        blob = json.dumps(payload)
        self.assertNotIn("Executable Now Rate", blob)
        if int(obs.get("n_7d") or 0) < 10:
            self.assertNotIn("healthy", obs)
            self.assertNotIn("executable_now_rate", obs)
            self.assertEqual(obs.get("reliability"), "unknown")


class HomepageHonestyTests(unittest.TestCase):
    def test_index_and_app_omit_invented_reliability(self):
        root = os.path.join(os.path.dirname(__file__), "..", "live402", "static")
        index = open(os.path.join(root, "index.html"), encoding="utf-8").read()
        app = open(os.path.join(root, "app.js"), encoding="utf-8").read()
        for blob in (index, app):
            self.assertNotIn("7d reliability", blob)
            self.assertNotIn("healthy", blob)
            self.assertNotIn("Executable Now Rate", blob)
        self.assertNotIn("objective", app)
        self.assertNotIn("most_reliable", app)


if __name__ == "__main__":
    unittest.main()
