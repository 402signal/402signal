"""Durable MainNet canary state machine. No live network. No MainNet txn."""

from __future__ import annotations

import base64
import os
import tempfile
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import payment
from live402.pq import ORIGIN_MAINNET, algo_anchor, canary, store
from live402.pq import checkpoint as ckpt
from tests.pq_test_env import clear_pq_env

_SIG = base64.b64encode(b"\x00" * 4 + b"\x22" * 64).decode("ascii")
_FALCON_PK = b"pk" + bytes(range(14))
_FALCON_SIG = b"sig" + bytes(range(29))


def _signed_note(size, root, origin=ORIGIN_MAINNET):
    body = ckpt.checkpoint_body(origin, int(size), bytes(root))
    return "%s\n%s %s %s\n" % (body, ckpt.EMDASH, origin, _SIG)


def _mainnet_signed(size, root, addr=None, fee=3000, fv=1, lv=1001):
    from live402 import algo_tx

    addr = addr or payment.DEFAULT_PAYTO_ALGORAND
    note = algo_anchor.encode_note(ORIGIN_MAINNET, int(size), bytes(root))
    gh = base64.b64decode(algo_anchor.MAINNET_GENESIS_HASH)
    txn = algo_tx.pay_txn(addr, addr, 0, fee, fv, lv, algo_anchor.MAINNET_GENESIS_ID, gh, note=note)
    return algo_tx.msgpack_encode(
        {"pqsig": {"pk": _FALCON_PK, "sch": "f1", "sig": _FALCON_SIG, "slt": 0}, "txn": txn}
    )


def _indexer(txid, size, root, addr=None):
    addr = addr or payment.DEFAULT_PAYTO_ALGORAND
    note = algo_anchor.encode_note(ORIGIN_MAINNET, int(size), bytes(root))
    return {
        "transaction": {
            "id": txid,
            "confirmed-round": 99,
            "genesis-id": algo_anchor.MAINNET_GENESIS_ID,
            "tx-type": "pay",
            "sender": addr,
            "fee": 3000,
            "note": base64.b64encode(note).decode("ascii"),
            "payment-transaction": {"amount": 0, "receiver": addr},
            "signature": {
                "pqsig": {
                    "scheme": "f1",
                    "salt": 0,
                    "public-key": base64.b64encode(_FALCON_PK).decode("ascii"),
                    "signature": base64.b64encode(_FALCON_SIG).decode("ascii"),
                }
            },
        }
    }


class CanaryStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clear_pq_env()
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN_MAINNET
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log-mainnet.sqlite")
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = payment.DEFAULT_PAYTO_ALGORAND
        os.environ["LIVE402_PQ_SIGNER_MAINNET_TOKEN"] = "named-not-valued"
        store.reset()
        store.append(b"canary-leaf")
        self.root = store.root(1)
        store.save_checkpoint(1, _signed_note(1, self.root))
        self.blob = _mainnet_signed(1, self.root)
        self.expected = algo_anchor.signed_txn_txid(self.blob)
        self.params = {
            "minFee": 1000,
            "fee": 0,
            "lastRound": 1,
            "genesisID": algo_anchor.MAINNET_GENESIS_ID,
            "genesisHash": algo_anchor.MAINNET_GENESIS_HASH,
        }
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        clear_pq_env()
        store.reset()
        self.tmp.cleanup()

    def _arm_send(self):
        os.environ["LIVE402_PQ_FALCON_MAINNET_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_MAINNET_CANARY"] = "1"
        os.environ["CONFIRM_MAINNET_CANARY"] = "I_UNDERSTAND"

    def _authorize(self):
        return canary.authorize(
            params=self.params,
            sign_fn=lambda _ident: self.blob,
        )

    def test_authorize_persists_before_send(self):
        row = self._authorize()
        self.assertEqual(canary.send_state_of(row), canary.STATE_AUTHORIZED)
        self.assertEqual(bytes(row["signed"]), self.blob)
        again = canary.authorize(params=self.params, sign_fn=lambda _i: (_ for _ in ()).throw(AssertionError("re-sign")))
        self.assertEqual(bytes(again["signed"]), self.blob)

    def test_default_refuses_to_send(self):
        row = self._authorize()
        posted = []
        with self.assertRaises(canary.CanaryError):
            canary.send_durable(row, authorize_human_canary=False, send_fn=lambda b: posted.append(b) or self.expected)
        self.assertEqual(posted, [])
        os.environ["LIVE402_PQ_FALCON_MAINNET_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_MAINNET_CANARY"] = "1"
        with self.assertRaises(canary.CanaryError):
            canary.send_durable(row, authorize_human_canary=True, send_fn=lambda b: posted.append(b) or self.expected)
        self.assertEqual(posted, [])

    def test_crash_before_post_no_duplicate(self):
        self._arm_send()
        row = self._authorize()
        posted = []
        with self.assertRaises(canary.CanaryError):
            canary.send_durable(
                row,
                authorize_human_canary=True,
                params=self.params,
                send_fn=lambda b: posted.append(b) or self.expected,
                crash_before_post=True,
            )
        self.assertEqual(posted, [])
        stored = store.authorized_at(1)
        self.assertEqual(canary.send_state_of(stored), canary.STATE_SEND_ATTEMPTED)
        self.assertEqual(stored["expected_txid"], self.expected)
        with self.assertRaises(canary.CanaryError):
            canary.send_durable(
                stored,
                authorize_human_canary=True,
                params=self.params,
                send_fn=lambda b: posted.append(b) or self.expected,
            )
        self.assertEqual(posted, [])

    def test_crash_after_socket_send_and_timeout(self):
        self._arm_send()
        row = self._authorize()
        posted = []

        def boom(blob):
            posted.append(bytes(blob))
            raise TimeoutError("provider timeout")

        with self.assertRaises(canary.CanaryError):
            canary.send_durable(row, authorize_human_canary=True, params=self.params, send_fn=boom)
        self.assertEqual(len(posted), 1)
        stored = store.authorized_at(1)
        self.assertEqual(canary.send_state_of(stored), canary.STATE_SEND_ATTEMPTED)
        with self.assertRaises(canary.CanaryError):
            canary.send_durable(
                stored,
                authorize_human_canary=True,
                params=self.params,
                send_fn=lambda b: posted.append(b) or self.expected,
            )
        self.assertEqual(len(posted), 1)

    def test_provider_accepts_response_lost_then_poll(self):
        self._arm_send()
        row = self._authorize()
        posted = []

        def lost(blob):
            posted.append(bytes(blob))
            raise ConnectionError("response lost")

        with self.assertRaises(canary.CanaryError):
            canary.send_durable(row, authorize_human_canary=True, params=self.params, send_fn=lost)
        self.assertEqual(len(posted), 1)
        stored = store.authorized_at(1)

        def fetch(txid):
            self.assertEqual(txid, self.expected)
            return _indexer(self.expected, 1, self.root)

        out = canary.send_durable(
            stored,
            authorize_human_canary=True,
            params=self.params,
            send_fn=lambda b: posted.append(b) or "Z" * 52,
            fetch_fn=fetch,
        )
        self.assertEqual(len(posted), 1)
        self.assertEqual(out["txid"], self.expected)
        self.assertEqual(canary.send_state_of(store.authorized_at(1)), canary.STATE_CONFIRMED)

    def test_restart_submitted_and_repeat_invocation(self):
        self._arm_send()
        row = self._authorize()
        posted = []

        def send(blob):
            posted.append(bytes(blob))
            return self.expected

        def missing(_txid):
            return None

        submitted = canary.send_durable(
            row,
            authorize_human_canary=True,
            params=self.params,
            send_fn=send,
            fetch_fn=missing,
        )
        self.assertEqual(canary.send_state_of(store.authorized_at(1)), canary.STATE_SUBMITTED)
        self.assertEqual(submitted["expected_txid"], self.expected)
        with self.assertRaises(canary.CanaryError):
            canary.send_durable(
                store.authorized_at(1),
                authorize_human_canary=True,
                params=self.params,
                send_fn=lambda b: posted.append(b) or self.expected,
                fetch_fn=missing,
            )
        self.assertEqual(len(posted), 1)

        def fetch(txid):
            return _indexer(txid, 1, self.root)

        confirmed = canary.send_durable(
            store.authorized_at(1),
            authorize_human_canary=True,
            params=self.params,
            send_fn=lambda b: posted.append(b) or self.expected,
            fetch_fn=fetch,
        )
        self.assertEqual(confirmed["txid"], self.expected)
        self.assertEqual(len(posted), 1)
        again = canary.send_durable(
            store.authorized_at(1),
            authorize_human_canary=True,
            params=self.params,
            send_fn=lambda b: posted.append(b) or self.expected,
            fetch_fn=fetch,
        )
        self.assertEqual(again["txid"], self.expected)
        self.assertEqual(len(posted), 1)

    def test_provider_txid_mismatch_is_security_failure(self):
        self._arm_send()
        row = self._authorize()
        with self.assertRaises(canary.CanarySecurityError):
            canary.send_durable(
                row,
                authorize_human_canary=True,
                params=self.params,
                send_fn=lambda _b: "C" * 52,
            )
        self.assertEqual(canary.send_state_of(store.authorized_at(1)), canary.STATE_SEND_ATTEMPTED)

    def test_incompatible_reauth_fail_closed(self):
        row = self._authorize()
        other_root = b"\x22" * 32
        self.assertFalse(
            canary.same_authorization_policy(
                row,
                origin=ORIGIN_MAINNET,
                tree_size=1,
                root=other_root,
                checkpoint=_signed_note(1, other_root),
            )
        )
        with store._lock:
            conn = store._connect()
            conn.execute(
                "UPDATE authorized_anchors SET root = ?, checkpoint = ? WHERE tree_size = 1",
                (other_root.hex(), _signed_note(1, other_root)),
            )
            conn.commit()
        with self.assertRaises(canary.CanarySecurityError):
            canary.authorize(params=self.params, sign_fn=lambda _i: self.blob)

    def test_both_flags_left_set_still_oneshot(self):
        self._arm_send()
        row = self._authorize()
        posted = []
        canary.send_durable(
            row,
            authorize_human_canary=True,
            params=self.params,
            send_fn=lambda b: posted.append(b) or self.expected,
            fetch_fn=lambda t: _indexer(t, 1, self.root),
        )
        self.assertEqual(len(posted), 1)
        canary.run(
            authorize_human_canary=True,
            params=self.params,
            sign_fn=lambda _i: self.blob,
            send_fn=lambda b: posted.append(b) or self.expected,
            fetch_fn=lambda t: _indexer(t, 1, self.root),
        )
        self.assertEqual(len(posted), 1)

    def test_cli_default_refuse(self):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "scripts" / "pq_mainnet_canary.py"
        spec = importlib.util.spec_from_file_location("pq_mainnet_canary", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        rc = mod.main([])
        self.assertEqual(rc, 2)

    def test_stale_snapshot_does_not_latch_send_attempted(self):
        self._arm_send()
        row = self._authorize()
        import json

        policy = json.loads(row["fee_policy"])
        policy["snapshot_at"] = 1
        store.save_authorized_checkpoint(
            tree_size=1,
            origin=row["origin"],
            root=row["root"],
            checkpoint=row["checkpoint"],
            request_id=row["request_id"],
            signed=row["signed"],
            at=row["at"],
            send_state=canary.STATE_AUTHORIZED,
            fee_policy=policy,
            fv=row["fv"],
            lv=row["lv"],
        )
        posted = []
        with self.assertRaises(canary.CanaryError):
            canary.send_durable(
                store.authorized_at(1),
                authorize_human_canary=True,
                send_fn=lambda b: posted.append(b) or self.expected,
                now=int(__import__("time").time()),
            )
        self.assertEqual(posted, [])
        self.assertEqual(canary.send_state_of(store.authorized_at(1)), canary.STATE_AUTHORIZED)

    def test_summary_has_no_secrets(self):
        row = self._authorize()
        info = canary.summary(row, router_sha="deadbeef")
        self.assertEqual(info["network"], "mainnet")
        self.assertEqual(info["amount"], 0)
        self.assertEqual(info["fee"], 3000)
        blob = str(info).lower()
        self.assertNotIn("mnemonic", blob)
        self.assertNotIn("named-not-valued", blob)
        self.assertNotIn("private", blob)


if __name__ == "__main__":
    unittest.main()
