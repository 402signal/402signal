"""Algorand Falcon construction only. No send. No private keys."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import algod, payment
from live402.pq import ORIGIN, algo_anchor, checkpoint, merkle, store, worker


class _FakeFalconSigner:
    """Stand-in for py-algorand-sdk Falcon1024AlgorandSigner(pk, signer_callback)."""

    def __init__(self, pk, signer_callback):
        self.pk = pk
        self.signer_callback = signer_callback

    def sign(self, unsigned_txn):
        return self.signer_callback(unsigned_txn)


class AlgorandConstructionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        worker.clear_queue()
        self.sender = payment.DEFAULT_PAYTO_ALGORAND
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        worker.clear_queue()
        store.reset()
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        self.tmp.cleanup()

    def test_note_is_84_bytes_and_round_trips_to_c2sp_body(self):
        root = merkle.mth([b"a"])
        note = algo_anchor.encode_note(ORIGIN, 1, root)
        self.assertEqual(len(note), 84)
        self.assertEqual(note[:11], b"402sg/pq1:b")
        self.assertEqual(note[11], 1)
        body = algo_anchor.c2sp_body_from_note(note, ORIGIN)
        parsed = checkpoint.parse_checkpoint_body(body)
        self.assertEqual(parsed["origin"], ORIGIN)
        self.assertEqual(parsed["tree_size"], 1)
        self.assertEqual(parsed["root"], root)
        again = algo_anchor.note_from_checkpoint_body(body)
        self.assertEqual(again, note)

    def test_payment_txn_fee_3000_not_submitted(self):
        store.append(b"leaf-one")
        root = store.root()
        note = algo_anchor.encode_note(ORIGIN, 1, root)
        params = algod.suggested_params()
        send_calls = []

        def fake_send(*_a, **_k):
            send_calls.append(True)
            raise AssertionError("must not submit")

        with patch.object(algo_anchor, "send_forbidden", fake_send):
            txn = algo_anchor.build_payment_txn(self.sender, note, params)
        self.assertEqual(txn["type"], "pay")
        self.assertEqual(txn["fee"], 3000)
        self.assertGreaterEqual(txn["fee"], 3000)
        self.assertTrue(txn.get("flatFee"))
        self.assertEqual(txn["snd"], txn["rcv"])
        self.assertNotIn("amt", txn)
        self.assertEqual(txn["note"], note)
        self.assertEqual(send_calls, [])

        called = []

        def callback(unsigned):
            called.append(unsigned)
            return b"pqsig-fixture"

        pk = b"\x00" * 32
        with patch.object(algo_anchor, "_falcon_sdk_signer", lambda _pk, cb: _FakeFalconSigner(_pk, cb)):
            sig = algo_anchor.isolated_sign(txn, callback, pk=pk)
        self.assertEqual(sig, b"pqsig-fixture")
        self.assertEqual(len(called), 1)
        self.assertIs(called[0], txn)
        self.assertEqual(send_calls, [])
        # Constructing the SDK-shaped signer must not imply a network send.
        sdk_like = _FakeFalconSigner(pk, callback)
        self.assertEqual(sdk_like.sign(txn), b"pqsig-fixture")

    def test_idle_does_not_build_when_size_unchanged(self):
        store.append(b"one")
        worker.save_anchor(1, 1)
        built = []

        def boom(*_a, **_k):
            built.append(True)
            raise AssertionError("must not build idle anchor")

        with patch.object(algo_anchor, "build_payment_txn", boom):
            self.assertFalse(worker.should_build(now=10**12, tree_size=1))
            self.assertIsNone(worker.enqueue_unsigned(now=10**12))
        self.assertEqual(built, [])
        self.assertEqual(worker.queued(), [])

    def test_sla_1000_leaves_or_15_min(self):
        worker.save_anchor(0, 1000)
        self.assertFalse(worker.should_build(now=1000 + 60, tree_size=1))
        self.assertTrue(worker.should_build(now=1000 + 15 * 60, tree_size=1))
        self.assertTrue(worker.should_build(now=1001, tree_size=1000))

    def test_worker_signs_via_callback_and_never_sends(self):
        store.append(b"one")
        worker.save_anchor(0, 0)
        self.assertTrue(worker.should_build(now=15 * 60, tree_size=1))
        item = worker.enqueue_unsigned(now=15 * 60)
        self.assertIsNotNone(item)
        sent = []

        def callback(unsigned):
            self.assertEqual(unsigned["fee"], 3000)
            return b"sig"

        def fake_send(*_a, **_k):
            sent.append(1)
            raise AssertionError("send")

        with patch.object(algo_anchor, "send_forbidden", fake_send):
            out = worker.process_one(callback, self.sender, algod.suggested_params(), now=15 * 60)
        self.assertIsNotNone(out)
        self.assertFalse(out["submitted"])
        self.assertEqual(out["status"], "pending")
        self.assertNotEqual(out["status"], "state_proof_covered")
        self.assertEqual(len(out["note"]), 84)
        self.assertEqual(sent, [])
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.never_state_proof_covered("state_proof_covered")


if __name__ == "__main__":
    unittest.main()
