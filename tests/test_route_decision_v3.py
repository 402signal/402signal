"""route_decision.v3 private evidence, commitment, reveal, receipt verify."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import time
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from live402 import payment, reputation, schema_fields
from live402.pq import ORIGIN, events, jcs, receipt, store


def _evidence(**over):
    ev = {
        "comparison": {
            "candidate_count": 2,
            "candidate_set_digest": "ab" * 32,
            "observation_batch_hash": "cd" * 32,
            "probe_batch_id": "batch-v3-1",
        },
        "decision": {
            "miss_reason": None,
            "outcome": "winner",
            "winner_url": "https://wx.example/forecast",
        },
        "evidence_version": 1,
        "observation": {
            "challenge_observed": True,
            "http_status": 402,
            "invocable": True,
            "latency_ms": 41,
            "live": True,
            "observed_at": "2026-09-01T11:00:00Z",
            "payable": True,
        },
        "policy": {
            "constraints": {"max_price_usd": 0.01, "networks": ["base"]},
            "objective": "cheapest",
            "unresolved": [],
        },
        "request": {"need": "weather under a cent", "url": None},
        "scoring": {
            "model_hash": reputation.model_hash(),
            "model_id": reputation.MODEL_ID,
        },
        "selected_payment": {
            "amount_atomic": "10000",
            "asset": payment.USDC_BASE,
            "network": payment.BASE_CAIP2,
            "payTo": payment.DEFAULT_PAYTO,
            "rail": "base",
            "scheme": "exact",
        },
    }
    ev.update(over)
    return events.canonicalize_private_evidence_v3(ev)


class V3EvidenceTests(unittest.TestCase):
    def test_public_leaf_is_minimal(self):
        leaf, reveal = events.route_decision_event_v3(evidence=_evidence(), ts=1756723344)
        self.assertEqual(leaf["type"], events.TYPE_ROUTE_DECISION_V3)
        self.assertEqual(set(leaf), {"type", "ts", "nonce", "commitment"})
        self.assertTrue(leaf["ts"].endswith("00Z"))
        self.assertGreaterEqual(len(leaf["nonce"]), 64)
        blob = json.dumps(leaf)
        for forbidden in (
            "weather under a cent",
            "wx.example",
            "payTo",
            "salt",
            "evidence",
            "live",
            "miss_reason",
            "anonymous",
            "unlinkable",
            payment.DEFAULT_PAYTO,
        ):
            self.assertNotIn(forbidden, blob)
        self.assertNotIn("salt", leaf)
        self.assertEqual(reveal["event_version"], events.TYPE_ROUTE_DECISION_V3)
        self.assertEqual(reveal["commitment"], leaf["commitment"])
        self.assertEqual(len(bytes.fromhex(reveal["salt"])), events.SALT_BYTES)
        self.assertTrue(events.verify_reveal_v3(leaf["commitment"], reveal))
        with self.assertRaises(events.PrivacyError):
            events.assert_public(dict(leaf, live=True))
        with self.assertRaises(events.PrivacyError):
            events.assert_public(dict(leaf, salt=reveal["salt"]))

    def test_v2_semantics_unchanged(self):
        leaf, reveal = events.route_decision_event_v2(
            need="secret weather in austin",
            url="https://example.com/x402",
            live=True,
            ts=1756627200,
        )
        self.assertEqual(leaf["type"], events.TYPE_ROUTE_DECISION_V2)
        self.assertIn("live", leaf)
        self.assertTrue(events.verify_reveal(leaf["commitment"], reveal))
        self.assertFalse(events.verify_reveal_v3(leaf["commitment"], reveal))

    def test_domain_separated_commitment(self):
        ev = _evidence()
        salt = bytes(range(32))
        got = events.commitment_hash_v3(ev, salt)
        manual = __import__("hashlib").sha256()
        manual.update(events.V3_DOMAIN)
        manual.update(jcs.canonicalize(ev))
        manual.update(salt)
        self.assertEqual(got, manual.hexdigest())
        other = events.commitment_hash_v2({"need": "x", "url": "", "prompt": "", "extra": {}}, salt)
        self.assertNotEqual(got, other)

    def test_verify_reveal_v3_fail_closed(self):
        leaf, reveal = events.route_decision_event_v3(evidence=_evidence())
        self.assertFalse(events.verify_reveal_v3(leaf["commitment"], None))
        self.assertFalse(events.verify_reveal_v3("aa", reveal))
        self.assertFalse(events.verify_reveal_v3(leaf["commitment"], {}))
        bad_ver = dict(reveal, event_version=events.TYPE_ROUTE_DECISION_V2)
        self.assertFalse(events.verify_reveal_v3(leaf["commitment"], bad_ver))
        self.assertFalse(events.verify_reveal_v3(leaf["commitment"], dict(reveal, salt="zz")))
        self.assertFalse(events.verify_reveal_v3(leaf["commitment"], dict(reveal, evidence="nope")))

    def test_candidate_set_digest_not_a_full_dump(self):
        compared = [
            {
                "url": "https://b.example/x",
                "rail": "base",
                "live": True,
                "invocable": False,
                "selected": False,
                "amount_atomic": "20000",
                "latency_ms": 9,
            },
            {
                "url": "https://a.example/x",
                "rail": "base",
                "live": True,
                "invocable": True,
                "selected": True,
                "amount_atomic": "10000",
                "latency_ms": 12,
                "selected_payment": {
                    "rail": "base",
                    "network": payment.BASE_CAIP2,
                    "scheme": "exact",
                    "asset": payment.USDC_BASE,
                    "amount_atomic": "10000",
                    "payTo": payment.DEFAULT_PAYTO,
                },
            },
        ]
        digest = events.candidate_set_digest(compared)
        self.assertEqual(len(digest), 64)
        again = events.candidate_set_digest(list(reversed(compared)))
        self.assertEqual(digest, again)
        self.assertIsNone(events.candidate_set_digest([]))
        self.assertIsNone(events.candidate_set_digest(None))

    def test_from_route_does_not_treat_catalog_as_observed(self):
        result = {
            "url": "https://wx.example/forecast",
            "live": True,
            "payable": True,
            "invocable": True,
            "challenge_observed": True,
            "status": 402,
            "latency_ms": 33,
            "objective": "best",
            "applied_constraints": {"networks": ["base"]},
            "unresolved_constraints": ["high reputation"],
            "batch_id": "cmp1",
            "compared": [{"url": "https://wx.example/forecast", "live": True, "selected": True}],
            "claimed": {"payTo": "0xclaimedclaimedclaimedclaimedclaimed000", "amount": "999"},
            "selected_payment": {
                "rail": "base",
                "network": payment.BASE_CAIP2,
                "scheme": "exact",
                "asset": payment.USDC_BASE,
                "amount_atomic": "10000",
                "payTo": payment.DEFAULT_PAYTO,
            },
        }
        ev = events.private_evidence_v3_from_route(result, {"need": "weather", "url": None})
        self.assertEqual(ev["evidence_version"], 1)
        self.assertEqual(ev["observation"]["live"], True)
        self.assertEqual(ev["selected_payment"]["payTo"], payment.DEFAULT_PAYTO)
        self.assertNotEqual(ev["selected_payment"]["payTo"], result["claimed"]["payTo"])
        self.assertIsInstance(ev["comparison"]["candidate_set_digest"], str)
        blob = json.dumps(ev)
        self.assertNotIn("PAYMENT-SIGNATURE", blob)
        self.assertNotIn("X-PAYMENT", blob)


class V3MutationAndReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        self.key = Ed25519PrivateKey.generate()
        self.vkey = receipt.configure_signer(self.key)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        receipt.configure_signer(None)
        store.reset()
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        self.tmp.cleanup()

    def _issue(self):
        ev = _evidence()
        leaf, reveal = events.route_decision_event_v3(evidence=ev, ts=int(time.time()))
        proof = receipt.issue(leaf)
        return leaf, reveal, proof

    def test_verify_route_receipt_roundtrip(self):
        leaf, reveal, proof = self._issue()
        out = receipt.verify_route_receipt(proof, reveal, self.vkey)
        self.assertEqual(out["body"]["origin"], ORIGIN)
        self.assertTrue(events.verify_reveal_v3(leaf["commitment"], reveal))

    def test_verify_route_receipt_fail_closed_missing_fields(self):
        _leaf, reveal, proof = self._issue()
        with self.assertRaises(receipt.ReceiptError):
            receipt.verify_route_receipt(None, reveal, self.vkey)
        with self.assertRaises(receipt.ReceiptError):
            receipt.verify_route_receipt(proof, None, self.vkey)
        with self.assertRaises(receipt.ReceiptError):
            receipt.verify_route_receipt(proof, dict(reveal, event_version="nope"), self.vkey)
        with self.assertRaises(receipt.ReceiptError):
            receipt.verify_route_receipt(dict(proof, leaf_hash=""), reveal, self.vkey)
        with self.assertRaises(receipt.ReceiptError):
            receipt.verify_route_receipt(proof, dict(reveal, salt=None), self.vkey)
        with self.assertRaises(receipt.ReceiptError):
            receipt.verify_route_receipt(proof, dict(reveal, evidence=None), self.vkey)

    def test_attach_to_route_uses_v3(self):
        out = receipt.attach_to_route(
            {
                "live": True,
                "url": "https://fixture.402signal.local/weather",
                "payable": True,
                "objective": "best",
                "selected_payment": {
                    "rail": "base",
                    "network": payment.BASE_CAIP2,
                    "scheme": "exact",
                    "asset": payment.USDC_BASE,
                    "amount_atomic": "10000",
                    "payTo": payment.DEFAULT_PAYTO,
                },
            },
            {"need": "private-need-text", "objective": "best"},
        )
        tr = out["pq_trust"]["transparency"]
        self.assertEqual(tr["leaf_type"], events.TYPE_ROUTE_DECISION_V3)
        receipt.verify_route_receipt(tr["receipt"], tr["reveal"], self.vkey)
        leaf = json.loads(store.leaf_at(tr["index"])["body"].decode("utf-8"))
        self.assertNotIn("private-need-text", json.dumps(leaf))
        self.assertEqual(tr["reveal"]["evidence"]["request"]["need"], "private-need-text")

    def _mutate_and_fail(self, reveal, path, value):
        tampered = copy.deepcopy(reveal)
        cur = tampered["evidence"]
        for key in path[:-1]:
            cur = cur[key]
        cur[path[-1]] = value
        self.assertFalse(
            events.verify_reveal_v3(reveal["commitment"], tampered),
            msg="mutation %s=%r still verified" % (path, value),
        )
        _leaf, _rev, proof = self._issue()
        with self.assertRaises(receipt.ReceiptError):
            receipt.verify_route_receipt(proof, tampered, self.vkey)

    def test_mutating_bound_fields_breaks_verification(self):
        _leaf, reveal, proof = self._issue()
        cases = [
            (("policy", "objective"), "fastest"),
            (("policy", "constraints"), {"networks": ["solana"]}),
            (("decision", "winner_url"), "https://other.example/x"),
            (("observation", "live"), False),
            (("observation", "payable"), False),
            (("observation", "invocable"), False),
            (("selected_payment", "network"), "solana"),
            (("selected_payment", "asset"), payment.USDC_SOLANA_MINT),
            (("selected_payment", "amount_atomic"), "20000"),
            (("selected_payment", "payTo"), "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
            (("comparison", "candidate_set_digest"), "ef" * 32),
            (("scoring", "model_hash"), "11" * 32),
        ]
        for path, value in cases:
            with self.subTest(path=path):
                tampered = copy.deepcopy(reveal)
                cur = tampered["evidence"]
                for key in path[:-1]:
                    cur = cur[key]
                cur[path[-1]] = value
                self.assertFalse(events.verify_reveal_v3(reveal["commitment"], tampered))
                with self.assertRaises(receipt.ReceiptError):
                    receipt.verify_route_receipt(proof, tampered, self.vkey)

    def test_docs_do_not_claim_anonymous(self):
        self.assertNotIn("anonymous", " ".join(schema_fields.v3_public_leaf_reveals()))
        self.assertIn("salt", schema_fields.v3_public_leaf_omits())
        self.assertIn("catalog_claimed", schema_fields.v3_unbound_fields())


if __name__ == "__main__":
    unittest.main()
