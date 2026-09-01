"""Algorand Falcon construction + TestNet-gated submit. No live network."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
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


def _testnet_params():
    return {
        "flatFee": True,
        "fee": 3000,
        "minFee": 3000,
        "firstValid": 1,
        "lastValid": 1001,
        "genesisID": algo_anchor.TESTNET_GENESIS_ID,
        "genesisHash": algo_anchor.TESTNET_GENESIS_HASH,
    }


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
        os.environ.pop("LIVE402_PQ_FALCON_ADDRESS", None)
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

    def test_validate_unsigned_anchor_accepts_pq1_and_rejects_forgeries(self):
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = self.sender
        note = algo_anchor.encode_note(ORIGIN, 1, merkle.mth([b"a"]))
        txn = algo_anchor.build_payment_txn(self.sender, note, _testnet_params())
        algo_anchor.validate_unsigned_anchor(txn)

        bad_amt = dict(txn)
        bad_amt["amt"] = 1
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_unsigned_anchor(bad_amt)

        bad_rcv = dict(txn)
        bad_rcv["rcv"] = os.urandom(32)
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_unsigned_anchor(bad_rcv)

        bad_gen = dict(txn)
        bad_gen["gen"] = algo_anchor.MAINNET_GENESIS_ID
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_unsigned_anchor(bad_gen)

        missing = dict(txn)
        missing.pop("note", None)
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_unsigned_anchor(missing)

        forged = dict(txn)
        forged["note"] = b"\x00" * algo_anchor.NOTE_LEN
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_unsigned_anchor(forged)

        rebuilt = algo_anchor.canonical_unsigned_anchor(txn)
        for key in ("close", "rekey", "lx", "grp"):
            self.assertNotIn(key, rebuilt)
            extra = dict(txn)
            extra[key] = os.urandom(32)
            with self.assertRaises(algo_anchor.AnchorError):
                algo_anchor.validate_unsigned_anchor(extra)
            with self.assertRaises(algo_anchor.AnchorError):
                algo_anchor.canonical_unsigned_anchor(extra)

    def test_idle_does_not_build_when_size_unchanged(self):
        store.append(b"one")
        store.save_confirmed_checkpoint(
            tree_size=1,
            origin=ORIGIN,
            root=store.root(1),
            txid="A" * 52,
            confirmed_round=10,
            at=1,
        )
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
        self.assertEqual(worker.last_confirmed()["size"], 0)
        self.assertFalse(worker.should_build(now=60, tree_size=1))
        self.assertTrue(worker.should_build(now=15 * 60, tree_size=1))
        self.assertTrue(worker.should_build(now=1, tree_size=1000))

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
            out = worker.process_one(callback, self.sender, _testnet_params(), now=15 * 60)
        self.assertIsNotNone(out)
        self.assertFalse(out["submitted"])
        self.assertEqual(out["status"], "pending")
        self.assertNotEqual(out["status"], "state_proof_covered")
        self.assertEqual(len(out["note"]), 84)
        self.assertEqual(sent, [])
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.never_state_proof_covered("state_proof_covered")


class TestNetSubmitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        worker.clear_queue()
        self.sender = payment.DEFAULT_PAYTO_ALGORAND
        self._env_keys = (
            "LIVE402_PQ_FALCON_NETWORK",
            "LIVE402_PQ_FALCON_BROADCAST",
            "LIVE402_PQ_FALCON_ADDRESS",
            "LIVE402_PQ_SIGNER_TOKEN",
        )
        for key in self._env_keys:
            os.environ.pop(key, None)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        worker.clear_queue()
        store.reset()
        for key in self._env_keys:
            os.environ.pop(key, None)
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        self.tmp.cleanup()

    def _arm_testnet(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = self.sender

    def test_default_send_forbidden_even_when_broadcast_env_set(self):
        self._arm_testnet()
        with self.assertRaises(RuntimeError) as ctx:
            algo_anchor.send_forbidden({})
        self.assertIn("forbidden", str(ctx.exception).lower())
        sent = []
        store.append(b"one")
        worker.save_anchor(0, 0)
        # process_one is still construction-only.
        worker.enqueue_unsigned(now=15 * 60)
        out = worker.process_one(lambda _u: b"sig", self.sender, _testnet_params(), now=15 * 60)
        self.assertIsNotNone(out)
        self.assertFalse(out["submitted"])
        self.assertEqual(sent, [])

    def test_testnet_broadcast_unset_never_posts(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        os.environ.pop("LIVE402_PQ_FALCON_BROADCAST", None)
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = self.sender
        store.append(b"one")
        worker.save_anchor(0, 0)
        sent = []

        def mock_send(blob):
            sent.append(blob)
            raise AssertionError("must not send")

        out = worker.maybe_submit(
            lambda _u: b"pqsig-testnet",
            self.sender,
            _testnet_params(),
            now=15 * 60,
            send_fn=mock_send,
        )
        self.assertIsNone(out)
        self.assertEqual(sent, [])
        self.assertFalse(
            algo_anchor.submit_allowed(
                signer_callback=lambda _u: b"sig",
                sender=self.sender,
                params=_testnet_params(),
            )
        )

    def test_idle_size_unchanged_does_not_send(self):
        self._arm_testnet()
        store.append(b"one")
        worker.save_anchor(1, 1)
        sent = []
        built = []

        def mock_send(blob):
            sent.append(blob)
            raise AssertionError("must not send idle")

        def boom(*_a, **_k):
            built.append(True)
            raise AssertionError("must not build idle")

        with patch.object(algo_anchor, "build_payment_txn", boom):
            out = worker.maybe_submit(
                lambda _u: b"sig",
                self.sender,
                _testnet_params(),
                now=10**12,
                send_fn=mock_send,
            )
        self.assertIsNone(out)
        self.assertEqual(sent, [])
        self.assertEqual(built, [])

    def test_mainnet_genesis_is_rejected(self):
        self._arm_testnet()
        store.append(b"one")
        worker.save_anchor(0, 0)
        sent = []
        built = []
        params = dict(algod.suggested_params())
        self.assertEqual(params.get("genesisID"), algo_anchor.MAINNET_GENESIS_ID)

        def mock_send(blob):
            sent.append(blob)
            raise AssertionError("must not send mainnet")

        def boom(*_a, **_k):
            built.append(True)
            raise AssertionError("must not build mainnet submit")

        with patch.object(algo_anchor, "build_payment_txn", boom):
            out = worker.maybe_submit(
                lambda _u: b"sig",
                self.sender,
                params,
                now=15 * 60,
                send_fn=mock_send,
            )
        self.assertIsNone(out)
        self.assertEqual(sent, [])
        self.assertEqual(built, [])
        self.assertFalse(
            algo_anchor.submit_allowed(
                signer_callback=lambda _u: b"sig",
                sender=self.sender,
                params=params,
            )
        )

    def test_missing_any_gate_does_not_build_or_send(self):
        store.append(b"one")
        worker.save_anchor(0, 0)
        sent = []
        built = []

        def mock_send(blob):
            sent.append(blob)
            raise AssertionError("must not send")

        def boom(*_a, **_k):
            built.append(True)
            raise AssertionError("must not build")

        cases = [
            {},  # default: no network, no broadcast
            {"LIVE402_PQ_FALCON_NETWORK": "testnet"},
            {"LIVE402_PQ_FALCON_NETWORK": "testnet", "LIVE402_PQ_FALCON_BROADCAST": "1"},
            {
                "LIVE402_PQ_FALCON_NETWORK": "mainnet",
                "LIVE402_PQ_FALCON_BROADCAST": "1",
                "LIVE402_PQ_FALCON_ADDRESS": self.sender,
            },
        ]
        for env in cases:
            for key in self._env_keys:
                os.environ.pop(key, None)
            os.environ.update(env)
            with patch.object(algo_anchor, "build_payment_txn", boom):
                out = worker.maybe_submit(
                    lambda _u: b"sig",
                    env.get("LIVE402_PQ_FALCON_ADDRESS"),
                    _testnet_params(),
                    now=15 * 60,
                    send_fn=mock_send,
                )
            self.assertIsNone(out, env)
        self.assertEqual(sent, [])
        self.assertEqual(built, [])

    def test_fixture_mode_without_mock_does_not_hit_network(self):
        self._arm_testnet()
        store.append(b"one")
        worker.save_anchor(0, 0)
        posted = []

        self.assertTrue(hasattr(algo_anchor, "_post_testnet"))
        self.assertIsNone(algo_anchor.send_if_allowed(b"STXN", send_fn=None, params=_testnet_params()))
        out = worker.maybe_submit(
            lambda _u: b"sig",
            self.sender,
            _testnet_params(),
            now=15 * 60,
            send_fn=None,
        )
        self.assertIsNone(out)
        self.assertEqual(posted, [])

    def test_fly_toml_is_single_app_no_signer_secrets(self):
        text = Path(__file__).resolve().parent.parent.joinpath("fly.toml").read_text(encoding="utf-8")
        vm_lines = [ln for ln in text.splitlines() if ln.strip() == "[[vm]]"]
        self.assertEqual(len(vm_lines), 1)
        self.assertFalse(any(ln.strip() == "[processes]" for ln in text.splitlines()))
        self.assertNotIn("falcon =", text)
        self.assertIn('processes = ["app"]', text)
        self.assertIn("No extra machines in this PR", text)
        self.assertIn("do not fly deploy", text.lower())
        self.assertIn("1gb", text)
        self.assertNotIn("LIVE402_PQ_FALCON_SK", text)
        self.assertNotIn("LIVE402_PQ_FALCON_BROADCAST", text)
        self.assertNotIn("LIVE402_PQ_SIGNER_TOKEN", text)
        self.assertNotIn("LIVE402_PQ_SIGNER_ENABLE", text)
        self.assertIn("LIVE402_PQ_FALCON_NETWORK = \"testnet\"", text)
        self.assertIn("OBHYXCUVOLSTZVBN5JUFIYBD4X4ZFIAFZMWMU2P45VBYGWT26MV34IFFIU", text)
        dockerfile = Path(__file__).resolve().parent.parent.joinpath("Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("LIVE402_PQ_FALCON_BROADCAST", dockerfile)
        self.assertNotIn("LIVE402_PQ_FALCON_SK", dockerfile)
        self.assertNotIn("LIVE402_PQ_SIGNER_TOKEN", dockerfile)

    def test_trust_root_not_mainnet_go_stays_true(self):
        from live402.pq import trust

        desc = trust.trust_root()
        self.assertTrue(desc["not_mainnet_go"])
        self.assertEqual(desc["falcon"]["allowed_broadcast"], "testnet")
        self.assertEqual(desc["falcon"]["network"], "testnet-v1.0")
        self.assertEqual(trust.falcon_allowed_broadcast(), "testnet")


if __name__ == "__main__":
    unittest.main()
