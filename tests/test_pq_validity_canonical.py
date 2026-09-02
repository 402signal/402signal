"""MainNet fv/lv exact canonical. No fv=1 fallback. No live network."""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import algo_tx, payment
from live402.pq import ORIGIN, ORIGIN_MAINNET, algo_anchor, canary, store
from live402.pq import checkpoint as ckpt
from tests.pq_test_env import clear_pq_env, falcon_f1_fixture_pk, falcon_f1_fixture_sig

_SIG = __import__("base64").b64encode(b"\x00" * 4 + b"\x22" * 64).decode("ascii")


class CanonicalValidityTests(unittest.TestCase):
    def setUp(self):
        clear_pq_env()
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = payment.DEFAULT_PAYTO_ALGORAND
        store.reset()
        self.addr = payment.DEFAULT_PAYTO_ALGORAND
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        store.reset()
        clear_pq_env()
        self.tmp.cleanup()

    def test_missing_last_round_rejected(self):
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            algo_anchor.canonical_validity({}, require_canonical=True)
        self.assertIn("lastround", str(ctx.exception).lower())
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.snapshot_last_round({}, require=True)
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.build_mainnet_payment_txn(
                algo_anchor.encode_note(ORIGIN_MAINNET, 1, b"\x11" * 32),
                {
                    "minFee": 1000,
                    "fee": 0,
                    "genesisID": algo_anchor.MAINNET_GENESIS_ID,
                    "genesisHash": algo_anchor.MAINNET_GENESIS_HASH,
                },
                address=self.addr,
            )
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_validity_window(1, 1001, {}, require_canonical=True)

    def test_fv_lv_plus_minus_one_rejected(self):
        params = {"lastRound": 12345, "minFee": 1000, "fee": 0}
        fv, lv = algo_anchor.canonical_validity(params, require_canonical=True)
        self.assertEqual((fv, lv), (12345, 13345))
        for first, last in ((12344, 13344), (12346, 13346), (12345, 13344), (12345, 13346)):
            with self.assertRaises(algo_anchor.AnchorError) as ctx:
                algo_anchor.validate_validity_window(first, last, params, require_canonical=True)
            text = str(ctx.exception).lower()
            self.assertTrue("canonical" in text or "out of range" in text, text)

    def test_span_over_max_txn_life_rejected(self):
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_validity_window(10, 10 + 1001, {"lastRound": 10})
        self.assertEqual(algo_anchor.MAX_VALIDITY_WINDOW, 1000)
        self.assertEqual(algo_anchor.CANONICAL_VALIDITY_WINDOW, 1000)
        self.assertEqual(algo_anchor.SNAPSHOT_MAX_AGE_S, 90)

    def test_testnet_fixtures_isolated(self):
        fv, lv = algo_anchor.canonical_validity({}, require_canonical=False)
        self.assertEqual((fv, lv), (1, 1001))
        algo_anchor.validate_validity_window(1, 1001, {}, require_canonical=False)
        note = algo_anchor.encode_note(ORIGIN, 1, b"\x11" * 32)
        gh = __import__("base64").b64decode(algo_anchor.TESTNET_GENESIS_HASH)
        txn = algo_tx.pay_txn(
            self.addr, self.addr, 0, 3000, 1, 1001, algo_anchor.TESTNET_GENESIS_ID, gh, note=note
        )
        blob = algo_tx.msgpack_encode(
            {
                "pqsig": {"pk": falcon_f1_fixture_pk(b"pk"), "sch": "f1", "sig": falcon_f1_fixture_sig(b"sig"), "slt": 0},
                "txn": txn,
            }
        )
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = self.addr
        out = algo_anchor.validate_signed_txn(
            blob,
            expected_origin=ORIGIN,
            expected_size=1,
            expected_root=b"\x11" * 32,
            expected_address=self.addr,
            expected_network="testnet",
        )
        self.assertEqual(out["fv"], 1)
        self.assertEqual(out["lv"], 1001)

    def test_secondary_range_is_not_the_canonical_check(self):
        params = {"lastRound": 100, "minFee": 1000, "fee": 0}
        algo_anchor.validate_validity_window(100, 1100, params, require_canonical=True)
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_validity_window(101, 1101, params, require_canonical=True)
        self.assertEqual(algo_anchor.FV_LOOKBACK, 10)
        self.assertEqual(algo_anchor.FV_LOOKAHEAD, 10)


class ExpiredPolicyBeforePostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clear_pq_env()
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN_MAINNET
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log-mainnet.sqlite")
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = payment.DEFAULT_PAYTO_ALGORAND
        os.environ["LIVE402_PQ_SIGNER_MAINNET_TOKEN"] = "named-not-valued"
        os.environ["LIVE402_PQ_FALCON_MAINNET_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_MAINNET_CANARY"] = "1"
        os.environ["CONFIRM_MAINNET_CANARY"] = "I_UNDERSTAND"
        store.reset()
        store.append(b"canary-leaf")
        self.root = store.root(1)
        body = ckpt.checkpoint_body(ORIGIN_MAINNET, 1, self.root)
        store.save_checkpoint(1, "%s\n%s %s %s\n" % (body, ckpt.EMDASH, ORIGIN_MAINNET, _SIG))
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        clear_pq_env()
        store.reset()
        self.tmp.cleanup()

    def _blob(self, fv=1, lv=1001, fee=3000):
        note = algo_anchor.encode_note(ORIGIN_MAINNET, 1, self.root)
        gh = __import__("base64").b64decode(algo_anchor.MAINNET_GENESIS_HASH)
        txn = algo_tx.pay_txn(
            payment.DEFAULT_PAYTO_ALGORAND,
            payment.DEFAULT_PAYTO_ALGORAND,
            0,
            fee,
            fv,
            lv,
            algo_anchor.MAINNET_GENESIS_ID,
            gh,
            note=note,
        )
        return algo_tx.msgpack_encode(
            {
                "pqsig": {"pk": falcon_f1_fixture_pk(b"pk"), "sch": "f1", "sig": falcon_f1_fixture_sig(b"sig"), "slt": 0},
                "txn": txn,
            }
        )

    def test_already_expired_policy_rejected_before_post(self):
        blob = self._blob(fv=1, lv=1001)
        posted = []
        policy = {
            "min_fee": 1000,
            "fee_per_byte": 0,
            "last_round": 5000,
            "fv": 1,
            "lv": 1001,
            "canonical_fee": 3000,
            "snapshot_at": 1_700_000_000,
            "snapshot_max_age_s": 90,
        }
        row = store.save_authorized_checkpoint(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=self.root,
            checkpoint=store.checkpoint_at(1),
            request_id="exp",
            signed=blob,
            at=1_700_000_000,
            send_state=canary.STATE_AUTHORIZED,
            fee_policy=policy,
            fv=1,
            lv=1001,
        )
        with self.assertRaises(canary.CanaryError) as ctx:
            canary.send_durable(
                row,
                authorize_human_canary=True,
                send_fn=lambda b: posted.append(b) or ("A" * 52),
                now=1_700_000_010,
            )
        self.assertIn("expired", str(ctx.exception).lower())
        self.assertEqual(posted, [])
        self.assertEqual(canary.send_state_of(store.authorized_at(1)), canary.STATE_AUTHORIZED)

    def test_stale_snapshot_rejected_before_post(self):
        blob = self._blob(fv=1, lv=1001)
        posted = []
        policy = {
            "min_fee": 1000,
            "fee_per_byte": 0,
            "last_round": 1,
            "fv": 1,
            "lv": 1001,
            "canonical_fee": 3000,
            "snapshot_at": 1_700_000_000,
            "snapshot_max_age_s": 90,
        }
        row = store.save_authorized_checkpoint(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=self.root,
            checkpoint=store.checkpoint_at(1),
            request_id="stale",
            signed=blob,
            at=1_700_000_000,
            send_state=canary.STATE_AUTHORIZED,
            fee_policy=policy,
            fv=1,
            lv=1001,
        )
        with self.assertRaises(canary.CanaryError) as ctx:
            canary.send_durable(
                row,
                authorize_human_canary=True,
                send_fn=lambda b: posted.append(b) or ("A" * 52),
                now=1_700_000_000 + 91,
            )
        self.assertIn("stale", str(ctx.exception).lower())
        self.assertEqual(posted, [])
        self.assertEqual(canary.send_state_of(store.authorized_at(1)), canary.STATE_AUTHORIZED)


if __name__ == "__main__":
    unittest.main()
