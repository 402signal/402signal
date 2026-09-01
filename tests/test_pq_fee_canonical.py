"""ONE SIZE RULE: deterministic Falcon envelope estimate only. No live network."""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import algo_tx, payment
from live402.pq import ORIGIN, ORIGIN_MAINNET, algo_anchor, store
from tests.pq_test_env import clear_pq_env


class DeterministicFeeTests(unittest.TestCase):
    def setUp(self):
        clear_pq_env()
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = payment.DEFAULT_PAYTO_ALGORAND
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = payment.DEFAULT_PAYTO_ALGORAND
        store.reset()
        self.addr = payment.DEFAULT_PAYTO_ALGORAND
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        store.reset()
        clear_pq_env()
        self.tmp.cleanup()

    def _note(self, origin=ORIGIN):
        return algo_anchor.encode_note(origin, 1, b"\x11" * 32)

    def _params(self, **extra):
        p = {
            "minFee": 1000,
            "fee": 0,
            "lastRound": 12345,
            "genesisID": algo_anchor.MAINNET_GENESIS_ID,
            "genesisHash": algo_anchor.MAINNET_GENESIS_HASH,
        }
        p.update(extra)
        return p

    def _signed(self, fee, fv, lv, sig=None, pk=None, origin=ORIGIN_MAINNET):
        note = self._note(origin)
        gh = __import__("base64").b64decode(algo_anchor.MAINNET_GENESIS_HASH)
        txn = algo_tx.pay_txn(
            self.addr, self.addr, 0, fee, fv, lv, algo_anchor.MAINNET_GENESIS_ID, gh, note=note
        )
        envelope = {
            "pqsig": {
                "pk": pk if pk is not None else (b"pk" + bytes(range(14))),
                "sch": "f1",
                "sig": sig if sig is not None else (b"sig" + bytes(range(29))),
                "slt": 0,
            },
            "txn": txn,
        }
        return algo_tx.msgpack_encode(envelope), txn

    def test_shorter_actual_sig_still_equals_router_fee(self):
        short_sig = b"s" * 64
        self.assertLess(len(short_sig), algo_anchor.FALCON_F1_SIG_MAX)
        blob, txn = self._signed(3000, 12345, 13345, sig=short_sig)
        self.assertLess(len(blob), algo_anchor.estimate_falcon_authorized_size(txn))
        params = self._params()
        want = algo_anchor.required_fee(params, unsigned=txn)
        from_signed = algo_anchor.required_fee(params, signed=blob)
        from_short_again = algo_anchor.required_fee(params, unsigned=txn)
        self.assertEqual(want, 3000)
        self.assertEqual(from_signed, want)
        self.assertEqual(from_short_again, want)
        out = algo_anchor.validate_signed_txn(
            blob,
            expected_origin=ORIGIN_MAINNET,
            expected_size=1,
            expected_root=b"\x11" * 32,
            expected_address=self.addr,
            expected_network="mainnet",
            params=params,
            require_canonical=True,
        )
        self.assertEqual(out["fee"], want)

    def test_router_does_not_shrink_fee_from_real_shorter_sig(self):
        short_sig = b"s" * 80
        max_sig = b"S" * algo_anchor.FALCON_F1_SIG_MAX
        short_blob, txn = self._signed(3000, 12345, 13345, sig=short_sig)
        max_blob, _txn = self._signed(3000, 12345, 13345, sig=max_sig)
        params = self._params(fee=2)
        from_short = algo_anchor.required_fee(params, signed=short_blob)
        from_max = algo_anchor.required_fee(params, signed=max_blob)
        from_unsigned = algo_anchor.required_fee(params, unsigned=txn)
        self.assertEqual(from_short, from_max)
        self.assertEqual(from_short, from_unsigned)
        self.assertGreaterEqual(from_short, algo_anchor.MIN_FEE)

    def test_signer_fee_manipulation_fails(self):
        blob, _txn = self._signed(5000, 12345, 13345)
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            algo_anchor.validate_signed_txn(
                blob,
                expected_origin=ORIGIN_MAINNET,
                expected_size=1,
                expected_root=b"\x11" * 32,
                expected_address=self.addr,
                expected_network="mainnet",
                params=self._params(),
                require_canonical=True,
            )
        self.assertIn("fee", str(ctx.exception).lower())

    def test_integer_encoding_boundary_stable_across_fee_range(self):
        for fee in (3000, 4096, 16384, 30000):
            encoded = algo_tx._mp_uint(fee)
            self.assertEqual(len(encoded), 3, fee)
        self.assertEqual(len(algo_tx._mp_uint(255)), 2)
        self.assertEqual(len(algo_tx._mp_uint(256)), 3)
        self.assertEqual(len(algo_tx._mp_uint(65535)), 3)
        self.assertEqual(len(algo_tx._mp_uint(65536)), 5)
        note = self._note(ORIGIN_MAINNET)
        gh = __import__("base64").b64decode(algo_anchor.MAINNET_GENESIS_HASH)
        low = algo_tx.pay_txn(
            self.addr, self.addr, 0, 3000, 12345, 13345, algo_anchor.MAINNET_GENESIS_ID, gh, note=note
        )
        high = algo_tx.pay_txn(
            self.addr, self.addr, 0, 30000, 12345, 13345, algo_anchor.MAINNET_GENESIS_ID, gh, note=note
        )
        self.assertEqual(
            algo_anchor.estimate_falcon_authorized_size(low),
            algo_anchor.estimate_falcon_authorized_size(high),
        )
        self.assertEqual(algo_anchor.MAX_FEE, 30000)

    def test_required_fee_never_uses_len_signed(self):
        blob, txn = self._signed(3000, 12345, 13345, sig=b"tiny")
        params = self._params(fee=3)
        estimate = algo_anchor.estimate_falcon_authorized_size(txn)
        self.assertGreater(estimate, len(blob))
        need = algo_anchor.required_fee(params, signed=blob)
        self.assertEqual(need, max(3 * estimate, 3000))
        self.assertNotEqual(need, max(3 * len(blob), 3000))


if __name__ == "__main__":
    unittest.main()
