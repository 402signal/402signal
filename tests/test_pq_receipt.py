"""Receipt / tlog-proof, crash-safety, and degraded /route."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from live402.pq import ORIGIN, checkpoint, events, merkle, receipt, store
from live402.pq.checkpoint import EMDASH
from live402.route import handle_route


class _Crash(RuntimeError):
    pass


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        self.key = Ed25519PrivateKey.generate()
        self.vkey = receipt.configure_signer(self.key)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        receipt.install_before_append_hook(None)
        store.install_after_durable_hook(None)
        receipt.configure_signer(None)
        store.reset()
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        self.tmp.cleanup()

    def test_checkpoint_parser_and_sign(self):
        root = merkle.empty_tree_hash()
        note = checkpoint.sign_checkpoint(ORIGIN, 0, root, self.key)
        parsed = checkpoint.parse_signed_note(note)
        body = checkpoint.parse_checkpoint_body(parsed["text"])
        self.assertEqual(body["origin"], ORIGIN)
        self.assertEqual(body["tree_size"], 0)
        self.assertEqual(body["root"], root)
        verified = checkpoint.verify_signed_note(note, self.vkey)
        self.assertEqual(verified["body"]["origin"], ORIGIN)
        self.assertIn(EMDASH, note)
        self.assertTrue(self.vkey.startswith(ORIGIN + "+"))

    def test_issue_receipt_after_durable_leaf(self):
        ev = events.route_decision_event(need="secret", ts=1756627200)
        proof = receipt.issue(ev)
        self.assertEqual(proof["index"], 0)
        self.assertEqual(proof["checkpoint_size"], 1)
        self.assertTrue(store.leaf_at(0))
        receipt.verify_receipt(proof, self.vkey)
        leaf = store.leaf_at(0)["body"]
        self.assertNotIn(b"secret", leaf)
        self.assertNotIn(b"need", leaf)

    def test_corrupt_proof_rejected(self):
        ev = events.route_decision_event(need="x", ts=1756627200)
        proof = receipt.issue(ev)
        bad = dict(proof)
        path = list(proof["inclusion_path"])
        if path:
            raw = bytearray(base64.b64decode(path[0]))
            raw[0] ^= 0xFF
            path[0] = base64.b64encode(bytes(raw)).decode("ascii")
            bad["inclusion_path"] = path
        else:
            bad["inclusion_path"] = [base64.b64encode(os.urandom(32)).decode("ascii")]
        with self.assertRaises(receipt.ReceiptError):
            receipt.verify_receipt(bad, self.vkey)

    def test_crash_after_queue_before_append_no_receipt(self):
        def boom(_body):
            raise _Crash("queued, not durable")

        receipt.install_before_append_hook(boom)
        ev = events.route_decision_event(need="hidden", ts=1756627201)
        with self.assertRaises(_Crash):
            receipt.issue(ev)
        self.assertEqual(store.size(), 0)
        self.assertEqual(store.latest_checkpoint(), "")

    def test_crash_after_durable_before_sign_no_dangling_promise(self):
        def boom(_idx, _body):
            raise _Crash("durable, unsigned")

        store.install_after_durable_hook(boom)
        ev = events.route_decision_event(need="hidden", ts=1756627202)
        with self.assertRaises(_Crash):
            receipt.issue(ev)
        self.assertEqual(store.size(), 1)
        self.assertIsNotNone(store.leaf_at(0))
        self.assertEqual(store.latest_checkpoint(), "")
        store.install_after_durable_hook(None)
        again = receipt.issue(ev)
        self.assertEqual(again["index"], 0)
        receipt.verify_receipt(again, self.vkey)

    def test_unavailable_is_not_pending(self):
        receipt.configure_signer(None)
        result = {"live": True, "url": "https://fixture.402signal.local/weather"}
        out = receipt.attach_to_route(result, {"need": "weather"})
        tr = out["pq_trust"]["transparency"]
        self.assertEqual(tr["status"], "logged_uncheckpointed")
        self.assertEqual(tr["state"], "logged_uncheckpointed")
        self.assertNotEqual(tr["status"], "pending")
        self.assertIsNotNone(store.leaf_at(tr["index"]))
        self.assertFalse(store.latest_checkpoint())
        self.assertFalse(out["payment_authorization"]["pq_native"])
        self.assertNotIn("pq_secure", out)

    def test_log_kill_switch_is_unavailable(self):
        os.environ["LIVE402_PQ_LOG"] = "0"
        try:
            result = {"live": True, "url": "https://fixture.402signal.local/weather"}
            out = receipt.attach_to_route(result, {"need": "weather"})
            tr = out["pq_trust"]["transparency"]
            self.assertEqual(tr["status"], "unavailable")
            self.assertNotEqual(tr["status"], "pending")
            self.assertNotIn("receipt", tr)
        finally:
            os.environ.pop("LIVE402_PQ_LOG", None)

    def test_pending_means_durable_and_signed_not_algorand(self):
        result = {"live": True, "url": "https://fixture.402signal.local/weather"}
        out = receipt.attach_to_route(result, {"need": "weather in austin"})
        tr = out["pq_trust"]["transparency"]
        self.assertEqual(tr["status"], "pending")
        self.assertEqual(tr["state"], "checkpoint_signed")
        self.assertIn("receipt", tr)
        self.assertIsNotNone(store.leaf_at(tr["index"]))
        self.assertTrue(store.latest_checkpoint())
        leaf = store.leaf_at(tr["index"])["body"].decode("utf-8")
        self.assertNotIn("austin", leaf)
        self.assertNotIn("weather in austin", leaf)
        self.assertNotIn("state_proof_covered", str(out))
        receipt.verify_receipt(tr["receipt"], self.vkey)
        self.assertTrue(tr["receipt"].get("leaf_hash"))
        self.assertTrue(events.verify_reveal_v3(tr["reveal"]["commitment"], tr["reveal"]))
        self.assertEqual(tr["reveal"]["event_version"], events.TYPE_ROUTE_DECISION_V3)
        self.assertNotIn("salt", json.loads(leaf))
        self.assertNotIn("need", json.loads(leaf))

    def test_route_still_succeeds_when_log_unavailable(self):
        receipt.configure_signer(None)
        os.environ["LOCAL_FREE"] = "1"
        try:
            code, body, _extra = handle_route(
                {"need": "weather", "url": "https://fixture.402signal.local/weather"},
                {},
                "https://402signal.com/route",
            )
            self.assertEqual(code, 200)
            self.assertTrue(body["live"])
            self.assertEqual(body["pq_trust"]["transparency"]["status"], "logged_uncheckpointed")
            self.assertFalse(body["payment_authorization"]["pq_native"])
            self.assertNotEqual(body["pq_trust"]["transparency"]["status"], "pending")
        finally:
            os.environ.pop("LOCAL_FREE", None)

    def test_route_pending_when_signer_configured(self):
        os.environ["LOCAL_FREE"] = "1"
        try:
            code, body, _extra = handle_route(
                {"need": "weather", "url": "https://fixture.402signal.local/weather"},
                {},
                "https://402signal.com/route",
            )
            self.assertEqual(code, 200)
            tr = body["pq_trust"]["transparency"]
            self.assertEqual(tr["status"], "pending")
            self.assertEqual(tr["log_origin"], ORIGIN)
            self.assertIn("checkpoint", tr["receipt"])
            self.assertFalse(body["payment_authorization"]["pq_native"])
            self.assertTrue("pq_secure" not in body or body.get("pq_secure") is not True)
        finally:
            os.environ.pop("LOCAL_FREE", None)


if __name__ == "__main__":
    unittest.main()
