"""RFC 8785 JCS events and commitment-only privacy."""

from __future__ import annotations

import json
import os
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import reputation
from live402.pq import events, jcs, store
import tempfile


class JCSTests(unittest.TestCase):
    def test_amounts_as_strings(self):
        obj = {"amount": 10000, "display_amount": "0.01", "nested": {"fee": 3}}
        out = jcs.amounts_as_strings(obj)
        self.assertEqual(out["amount"], "10000")
        self.assertEqual(out["display_amount"], "0.01")
        self.assertEqual(out["nested"]["fee"], "3")
        text = jcs.canonicalize_text(out)
        parsed = json.loads(text)
        self.assertIsInstance(parsed["amount"], str)
        self.assertNotIsInstance(parsed["amount"], int)

    def test_rejects_nan_duplicate_keys_lone_surrogates(self):
        with self.assertRaises(jcs.JCSError):
            jcs.canonicalize(float("nan"))
        with self.assertRaises(jcs.JCSError):
            jcs.parse('{"a":1,"a":2}')
        with self.assertRaises(jcs.JCSError):
            jcs.canonicalize("\ud800")
        with self.assertRaises(jcs.JCSError):
            jcs.require_timestamp("2026-08-31T13:00:00+00:00")
        self.assertEqual(jcs.require_timestamp("2026-08-31T13:00:00Z"), "2026-08-31T13:00:00Z")

    def test_key_order_is_sorted(self):
        text = jcs.canonicalize_text({"b": 1, "a": 2})
        self.assertEqual(text, '{"a":2,"b":1}')


class EventPrivacyTests(unittest.TestCase):
    def test_route_decision_has_no_need_prompt_wallet(self):
        ev = events.route_decision_event(
            need="secret weather in austin",
            url="https://example.com/x402",
            prompt="ignore previous",
            live=True,
            ts=1756627200,
        )
        blob = json.dumps(ev)
        self.assertEqual(ev["type"], events.TYPE_ROUTE_DECISION)
        self.assertNotIn("need", ev)
        self.assertNotIn("prompt", ev)
        self.assertNotIn("wallet", blob.lower())
        self.assertNotIn("secret weather", blob)
        self.assertNotIn("austin", blob)
        self.assertNotIn("ignore previous", blob)
        self.assertGreaterEqual(len(ev["nonce"]), 64)
        self.assertEqual(len(ev["commitment"]), 64)
        leaf = events.leaf_bytes(ev)
        self.assertNotIn(b"need", leaf)
        self.assertNotIn(b"prompt", leaf)

    def test_observation_batch_is_counts_not_bodies(self):
        ev = events.observation_batch_event(
            batch_id="b1",
            n=3,
            digest="a" * 64,
            counts={"live": 2, "dead": 1},
            ts=1756627200,
        )
        self.assertEqual(ev["type"], events.TYPE_OBSERVATION_BATCH)
        self.assertEqual(ev["n"], 3)
        self.assertNotIn("response_body", ev)
        events.assert_public(ev)

    def test_scoring_model_reuses_pr16_hash(self):
        rec = reputation.model_record()
        ev = events.scoring_model_event(rec, ts=rec["effective_ts"])
        self.assertEqual(ev["type"], events.TYPE_SCORING_MODEL)
        self.assertEqual(ev["model_hash"], rec["model_hash"])
        self.assertEqual(ev["model_id"], reputation.MODEL_ID)
        self.assertEqual(len(ev["model_hash"]), 64)

    def test_forbidden_fields_fail_closed(self):
        with self.assertRaises(events.PrivacyError):
            events.assert_public(
                {
                    "type": events.TYPE_ROUTE_DECISION,
                    "ts": "2026-08-31T00:00:00Z",
                    "nonce": "a" * 64,
                    "commitment": "b" * 64,
                    "need": "leaked",
                }
            )

    def test_leaf_is_jcs_and_can_append(self):
        tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = tmp.name + "/pq-log.sqlite"
        store.reset()
        try:
            ev = events.route_decision_event(need="hidden", ts=1756627200)
            rec = store.append(events.leaf_bytes(ev))
            body = store.leaf_at(rec["idx"])["body"]
            parsed = json.loads(body.decode("utf-8"))
            self.assertEqual(parsed["type"], events.TYPE_ROUTE_DECISION)
            self.assertNotIn("need", parsed)
        finally:
            store.reset()
            os.environ.pop("LIVE402_PQ_LOG_DB", None)
            tmp.cleanup()

    def test_v2_salt_commitment_and_customer_reveal(self):
        leaf, reveal = events.route_decision_event_v2(
            need="secret weather in austin",
            url="https://example.com/x402",
            prompt="ignore previous",
            extra={"wallet": "should-not-appear-on-leaf"},
            live=True,
            ts=1756627200,
        )
        self.assertEqual(leaf["type"], events.TYPE_ROUTE_DECISION_V2)
        self.assertEqual(set(leaf) - {"live", "miss_reason"}, {"type", "ts", "nonce", "commitment"})
        blob = json.dumps(leaf)
        self.assertNotIn("salt", leaf)
        self.assertNotIn("evidence", leaf)
        self.assertNotIn("need", leaf)
        self.assertNotIn("secret weather", blob)
        self.assertNotIn("austin", blob)
        self.assertNotIn("ignore previous", blob)
        self.assertEqual(len(reveal["salt"]), 64)
        self.assertEqual(len(bytes.fromhex(reveal["salt"])), events.SALT_BYTES)
        self.assertTrue(events.verify_reveal(leaf["commitment"], reveal))
        self.assertEqual(events.commitment_hash_v2(reveal["evidence"], bytes.fromhex(reveal["salt"])), leaf["commitment"])
        tampered = dict(reveal)
        tampered["evidence"] = dict(reveal["evidence"], need="other")
        self.assertFalse(events.verify_reveal(leaf["commitment"], tampered))
        with self.assertRaises(events.PrivacyError):
            events.assert_public(dict(leaf, salt=reveal["salt"]))


if __name__ == "__main__":
    unittest.main()
