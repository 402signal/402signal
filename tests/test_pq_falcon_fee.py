"""Falcon fee derivation A-G. No live network. No MainNet transaction."""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import payment
from live402.pq import ORIGIN, ORIGIN_MAINNET, algo_anchor, store
from live402 import algo_tx
from tests.pq_test_env import clear_pq_env


class FalconFeeTests(unittest.TestCase):
    def setUp(self):
        clear_pq_env()
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        self.sender = payment.DEFAULT_PAYTO_ALGORAND
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = self.sender
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        store.reset()
        clear_pq_env()
        self.tmp.cleanup()

    def _note(self):
        return algo_anchor.encode_note(ORIGIN, 1, b"\x11" * 32)

    def _uncongested(self, **extra):
        p = {
            "minFee": 1000,
            "fee": 0,
            "firstValid": 1,
            "lastValid": 1001,
            "genesisID": algo_anchor.TESTNET_GENESIS_ID,
            "genesisHash": algo_anchor.TESTNET_GENESIS_HASH,
        }
        p.update(extra)
        return p

    def test_a_uncongested_is_3000_not_1000(self):
        fee = algo_anchor.required_fee(self._uncongested())
        self.assertEqual(fee, 3000)
        self.assertNotEqual(fee, 1000)
        txn = algo_anchor.build_payment_txn(self.sender, self._note(), self._uncongested())
        self.assertEqual(txn["fee"], 3000)

    def test_b_fee_per_byte_zero_is_falcon_min(self):
        self.assertEqual(algo_anchor.required_fee({"minFee": 1000, "fee": 0}), 3000)
        self.assertEqual(algo_anchor.required_fee({"minFee": 1000, "feePerByte": 0}), 3000)
        self.assertEqual(algo_anchor.falcon_min_fee({"minFee": 1000}), 3000)

    def test_c_congestion_uses_size_times_fee_per_byte(self):
        draft = algo_anchor.build_payment_txn(self.sender, self._note(), self._uncongested())
        size = algo_anchor.estimate_falcon_authorized_size(draft)
        self.assertGreater(size, 1793 + 1423)
        fpb = 2
        need = max(fpb * size, 3000)
        self.assertGreater(need, 3000)
        self.assertLessEqual(need, algo_anchor.MAX_FEE)
        params = self._uncongested(fee=fpb)
        got = algo_anchor.required_fee(params, unsigned=draft)
        self.assertEqual(got, need)
        txn = algo_anchor.build_payment_txn(self.sender, self._note(), params)
        self.assertEqual(txn["fee"], need)

    def test_d_over_cap_fail_closed(self):
        draft = algo_anchor.build_payment_txn(self.sender, self._note(), self._uncongested())
        size = algo_anchor.estimate_falcon_authorized_size(draft)
        fpb = (algo_anchor.MAX_FEE // size) + 1
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            algo_anchor.required_fee(self._uncongested(fee=fpb), unsigned=draft)
        self.assertIn("cap", str(ctx.exception).lower())
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.build_payment_txn(self.sender, self._note(), self._uncongested(fee=fpb))
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.build_mainnet_payment_txn(
                algo_anchor.encode_note(ORIGIN_MAINNET, 1, b"\x11" * 32),
                {
                    "minFee": 1000,
                    "fee": fpb,
                    "lastRound": 1,
                    "genesisID": algo_anchor.MAINNET_GENESIS_ID,
                    "genesisHash": algo_anchor.MAINNET_GENESIS_HASH,
                },
                address=self.sender,
            )

    def test_e_malformed_and_negative_fail_closed(self):
        for params in (
            {"minFee": -1},
            {"minFee": 0},
            {"minFee": "nope"},
            {"minFee": 1000, "fee": -5},
            {"minFee": 1000, "feePerByte": -1},
        ):
            with self.assertRaises(algo_anchor.AnchorError):
                algo_anchor.required_fee(params)

    def test_f_caller_cannot_select_fee(self):
        fixture = self._uncongested(flatFee=True, fee=12345)
        txn = algo_anchor.build_payment_txn(self.sender, self._note(), fixture)
        self.assertEqual(txn["fee"], 3000)
        self.assertNotEqual(txn["fee"], 12345)
        inbound = dict(txn)
        inbound["fee"] = 1000
        rebuilt = algo_anchor.rebuild_unsigned_anchor(inbound, params=self._uncongested())
        self.assertEqual(rebuilt["fee"], 3000)
        self.assertNotEqual(rebuilt["fee"], 1000)
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = self.sender
        main = algo_anchor.build_mainnet_payment_txn(
            algo_anchor.encode_note(ORIGIN_MAINNET, 1, b"\x11" * 32),
            {
                "minFee": 1000,
                "fee": 0,
                "flatFee": True,
                "callerFee": 9999,
                "lastRound": 1,
                "genesisID": algo_anchor.MAINNET_GENESIS_ID,
                "genesisHash": algo_anchor.MAINNET_GENESIS_HASH,
            },
            address=self.sender,
        )
        self.assertEqual(main["fee"], 3000)

    def test_g_signed_txn_validator_checks_fee_range_and_cap(self):
        addr = self.sender
        root = b"\x11" * 32
        note = algo_anchor.encode_note(ORIGIN, 1, root)
        gh = __import__("base64").b64decode(algo_anchor.TESTNET_GENESIS_HASH)
        too_small = algo_tx.pay_txn(addr, addr, 0, 1000, 1, 1001, algo_anchor.TESTNET_GENESIS_ID, gh, note=note)
        blob_small = algo_tx.msgpack_encode(
            {
                "pqsig": {"pk": b"pk" + bytes(range(14)), "sch": "f1", "sig": b"sig" + bytes(range(29)), "slt": 0},
                "txn": too_small,
            }
        )
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            algo_anchor.validate_signed_txn(
                blob_small,
                expected_origin=ORIGIN,
                expected_size=1,
                expected_root=root,
                expected_address=addr,
                expected_network="testnet",
            )
        self.assertIn("fee", str(ctx.exception).lower())
        over = algo_tx.pay_txn(addr, addr, 0, 30001, 1, 1001, algo_anchor.TESTNET_GENESIS_ID, gh, note=note)
        blob_over = algo_tx.msgpack_encode(
            {
                "pqsig": {"pk": b"pk" + bytes(range(14)), "sch": "f1", "sig": b"sig" + bytes(range(29)), "slt": 0},
                "txn": over,
            }
        )
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            algo_anchor.validate_signed_txn(
                blob_over,
                expected_origin=ORIGIN,
                expected_size=1,
                expected_root=root,
                expected_address=addr,
                expected_network="testnet",
            )
        self.assertIn("fee", str(ctx.exception).lower())
        ok = algo_tx.pay_txn(addr, addr, 0, 3000, 1, 1001, algo_anchor.TESTNET_GENESIS_ID, gh, note=note)
        blob_ok = algo_tx.msgpack_encode(
            {
                "pqsig": {"pk": b"pk" + bytes(range(14)), "sch": "f1", "sig": b"sig" + bytes(range(29)), "slt": 0},
                "txn": ok,
            }
        )
        out = algo_anchor.validate_signed_txn(
            blob_ok,
            expected_origin=ORIGIN,
            expected_size=1,
            expected_root=root,
            expected_address=addr,
            expected_network="testnet",
        )
        self.assertEqual(out["fee"], 3000)


if __name__ == "__main__":
    unittest.main()
