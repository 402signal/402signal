"""Local Algorand txid equals official SDK get_txid vectors. No live network."""

from __future__ import annotations

import base64
import os
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import algo_tx, payment
from live402.pq import algo_anchor

# Official py-algorand-sdk 2.11.1 PaymentTxn.get_txid() vectors.
# Address N2JSJZCSORMYGYO2NSIYRUEMBFRHEOMYODVXV2MXYYHB5H2JVUGG6NJ4NQ
# Do not add py-algorand-sdk to requirements.txt; these are baked.
_ADDR = "N2JSJZCSORMYGYO2NSIYRUEMBFRHEOMYODVXV2MXYYHB5H2JVUGG6NJ4NQ"
_PQ1_NOTE = b"402sg/pq1:b" + bytes([1]) + (b"\x00" * 32) + (1).to_bytes(8, "big") + (b"\x11" * 32)
_ORDINARY = "5UVIMGZKH6CZHOVIW5KR2TH4AR4B2BWBW6Q7DJO5K7P5Z2KTOQ2A"
_PQ1 = "RGQMSYUUSORNJSGMREDBZIYWN6XAGIFGDD2U6IPJGVOHDA2SPGDA"
_TESTNET = "VG223KSDPFDQWM5IGUCUTI5CDSUSMVWSRNPCWKK3G7DTPSXIY5CQ"
_CANARY = "HYXTMHHSFB4FIPTXJZQD6K3QYDHUVULP56Y3AVPKCNCXYYPUGHVQ"


class OfficialTxidParityTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(payment.DEFAULT_PAYTO_ALGORAND, _ADDR)
        self.assertEqual(len(_PQ1_NOTE), 84)

    def test_ordinary_mainnet_matches_official_sdk(self):
        gh = base64.b64decode(algo_anchor.MAINNET_GENESIS_HASH)
        txn = algo_tx.pay_txn(_ADDR, _ADDR, 0, 1000, 1000, 2000, "mainnet-v1.0", gh)
        self.assertEqual(algo_tx.txid_from_unsigned(txn), _ORDINARY)

    def test_pq1_mainnet_matches_official_sdk(self):
        gh = base64.b64decode(algo_anchor.MAINNET_GENESIS_HASH)
        txn = algo_tx.pay_txn(
            _ADDR, _ADDR, 0, 3000, 12345, 13345, "mainnet-v1.0", gh, note=_PQ1_NOTE
        )
        self.assertEqual(algo_tx.txid_from_unsigned(txn), _PQ1)
        signed = algo_tx.msgpack_encode(
            {
                "pqsig": {
                    "pk": b"\x00" * algo_anchor.FALCON_F1_PK_LEN,
                    "sch": "f1",
                    "sig": b"\x11" * 64,
                    "slt": 0,
                },
                "txn": txn,
            }
        )
        self.assertEqual(algo_tx.txid_from_signed(signed), _PQ1)
        self.assertEqual(algo_anchor.signed_txn_txid(signed), _PQ1)

    def test_testnet_fixture_isolated(self):
        gh = base64.b64decode(algo_anchor.TESTNET_GENESIS_HASH)
        txn = algo_tx.pay_txn(
            _ADDR, _ADDR, 0, 3000, 1, 1001, "testnet-v1.0", gh, note=_PQ1_NOTE
        )
        self.assertEqual(algo_tx.txid_from_unsigned(txn), _TESTNET)
        self.assertNotEqual(_TESTNET, _PQ1)

    def test_canary_fee_fv_lv_vector(self):
        """Canary-shaped MainNet PQ1: fee=3000, fv=50000, lv=51000."""
        gh = base64.b64decode(algo_anchor.MAINNET_GENESIS_HASH)
        txn = algo_tx.pay_txn(
            _ADDR, _ADDR, 0, 3000, 50000, 51000, "mainnet-v1.0", gh, note=_PQ1_NOTE
        )
        self.assertEqual(algo_tx.txid_from_unsigned(txn), _CANARY)
        signed = algo_tx.msgpack_encode(
            {"pqsig": {"pk": b"pk", "sch": "f1", "sig": b"sig", "slt": 0}, "txn": txn}
        )
        self.assertEqual(algo_tx.txid_from_signed(signed), _CANARY)

    def test_txid_is_payload_not_falcon_hash(self):
        gh = base64.b64decode(algo_anchor.MAINNET_GENESIS_HASH)
        txn = algo_tx.pay_txn(
            _ADDR, _ADDR, 0, 3000, 12345, 13345, "mainnet-v1.0", gh, note=_PQ1_NOTE
        )
        a = algo_tx.msgpack_encode(
            {"pqsig": {"pk": b"aa", "sch": "f1", "sig": b"one", "slt": 0}, "txn": txn}
        )
        b = algo_tx.msgpack_encode(
            {"pqsig": {"pk": b"bb", "sch": "f1", "sig": b"two", "slt": 1}, "txn": txn}
        )
        self.assertEqual(algo_tx.txid_from_signed(a), algo_tx.txid_from_signed(b))
        self.assertEqual(algo_tx.txid_from_signed(a), _PQ1)


if __name__ == "__main__":
    unittest.main()
