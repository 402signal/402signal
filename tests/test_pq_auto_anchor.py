"""Automatic MainNet anchoring: default-off and one-shot durability tests."""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import algo_tx, payment
from live402.pq import (
    ORIGIN_MAINNET,
    algo_anchor,
    auto_anchor,
    canary,
    monitor,
    store,
    trust,
    worker,
)
from live402.pq import checkpoint as ckpt
from tests.pq_test_env import (
    clear_pq_env,
    falcon_f1_fixture_pk,
    falcon_f1_fixture_sig,
)

_ADDR = payment.DEFAULT_PAYTO_ALGORAND
_SIG = base64.b64encode(b"\x00" * 4 + b"\x33" * 64).decode("ascii")
_PK = falcon_f1_fixture_pk(b"auto")
_FALCON_SIG = falcon_f1_fixture_sig(b"auto")


def _checkpoint(size: int, root: bytes) -> str:
    body = ckpt.checkpoint_body(ORIGIN_MAINNET, size, root)
    return "%s\n%s %s %s\n" % (body, ckpt.EMDASH, ORIGIN_MAINNET, _SIG)


def _signed(size: int, root: bytes, *, fv: int = 100, lv: int = 1100) -> bytes:
    note = algo_anchor.encode_note(ORIGIN_MAINNET, size, root)
    txn = algo_tx.pay_txn(
        _ADDR,
        _ADDR,
        0,
        3000,
        fv,
        lv,
        algo_anchor.MAINNET_GENESIS_ID,
        base64.b64decode(algo_anchor.MAINNET_GENESIS_HASH),
        note=note,
    )
    return algo_tx.msgpack_encode(
        {
            "pqsig": {"pk": _PK, "sch": b"f1", "sig": _FALCON_SIG, "slt": 0},
            "txn": txn,
        }
    )


def _indexer(txid: str, size: int, root: bytes, *, fv: int = 100, lv: int = 1100):
    return {
        "transaction": {
            "id": txid,
            "confirmed-round": 123,
            "genesis-id": algo_anchor.MAINNET_GENESIS_ID,
            "genesis-hash": algo_anchor.MAINNET_GENESIS_HASH,
            "tx-type": "pay",
            "sender": _ADDR,
            "fee": 3000,
            "first-valid": fv,
            "last-valid": lv,
            "note": base64.b64encode(
                algo_anchor.encode_note(ORIGIN_MAINNET, size, root)
            ).decode("ascii"),
            "payment-transaction": {"amount": 0, "receiver": _ADDR},
            "signature": {
                "pqsig": {
                    "scheme": "f1",
                    "salt": 0,
                    "public-key": base64.b64encode(_PK).decode("ascii"),
                    "signature": base64.b64encode(_FALCON_SIG).decode("ascii"),
                }
            },
        }
    }


class AutoAnchorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clear_pq_env()
        os.environ["LIVE402_FIXTURE"] = "1"
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN_MAINNET
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(
            self.tmp.name, "pq-log-mainnet.sqlite"
        )
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = _ADDR
        os.environ["LIVE402_PQ_SIGNER_MAINNET_TOKEN"] = "fixture-token"
        store.reset()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        clear_pq_env()
        os.environ["LIVE402_FIXTURE"] = "1"
        store.reset()
        self.tmp.cleanup()

    def _arm_auto(self):
        os.environ["LIVE402_PQ_FALCON_MAINNET_AUTO"] = "1"
        os.environ["LIVE402_PQ_FALCON_MAINNET_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_CONFIRM_PROVIDER"] = "tatum"
        os.environ["LIVE402_PQ_CONFIRM_TATUM_API_KEY"] = "fixture-key"

    def _leaf(self):
        store.append(b"automatic-leaf")
        root = store.root(1)
        store.save_checkpoint(1, _checkpoint(1, root))
        return root

    def _params(self):
        return {
            "minFee": 1000,
            "fee": 0,
            "lastRound": 100,
            "firstValid": 100,
            "lastValid": 1100,
            "genesisID": algo_anchor.MAINNET_GENESIS_ID,
            "genesisHash": algo_anchor.MAINNET_GENESIS_HASH,
        }

    def test_default_off_and_kill_switch_never_send(self):
        self._leaf()
        sent = []
        self.assertFalse(algo_anchor.automatic_mainnet_enabled())
        for value in ("", "true", "yes", "01", "2"):
            os.environ["LIVE402_PQ_FALCON_MAINNET_AUTO"] = value
            self.assertFalse(algo_anchor.automatic_mainnet_enabled(), value)
        self.assertIsNone(worker.tick(now=10_000, send_fn=lambda b: sent.append(b)))
        self.assertEqual(sent, [])
        self._arm_auto()
        os.environ["LIVE402_PQ_FALCON_MAINNET_AUTO_KILL"] = "1"
        self.assertFalse(algo_anchor.automatic_mainnet_enabled())
        self.assertIsNone(worker.tick(now=10_000, send_fn=lambda b: sent.append(b)))
        self.assertEqual(sent, [])

    def test_public_policy_reports_only_effective_automatic_mode(self):
        desc = trust._public_descriptor_mainnet()
        self.assertEqual(desc["falcon"]["allowed_broadcast"], "none")
        self.assertTrue(desc["not_mainnet_go"])
        self._arm_auto()
        desc = trust._public_descriptor_mainnet()
        self.assertEqual(desc["falcon"]["allowed_broadcast"], "mainnet")
        self.assertFalse(desc["not_mainnet_go"])
        snap = monitor.snapshot()
        self.assertTrue(snap["broadcast"]["automatic_active"])
        self.assertFalse(snap["trust"]["not_mainnet_go"])
        os.environ["LIVE402_PQ_FALCON_MAINNET_CANARY"] = "1"
        desc = trust._public_descriptor_mainnet()
        self.assertEqual(desc["falcon"]["allowed_broadcast"], "none")
        self.assertTrue(desc["not_mainnet_go"])

    def test_trigger_is_15_minutes_or_1000_leaves(self):
        self.assertFalse(
            auto_anchor._eligible(delta=1, unanchored_since=1000, now=1899)
        )
        self.assertTrue(
            auto_anchor._eligible(delta=1, unanchored_since=1000, now=1900)
        )
        self.assertTrue(
            auto_anchor._eligible(delta=1000, unanchored_since=0, now=1000)
        )
        self.assertFalse(
            auto_anchor._eligible(delta=0, unanchored_since=1, now=10_000)
        )

    def test_unchanged_observation_does_not_write(self):
        self._leaf()
        first = store.automation_observe(tree_size=1, confirmed_size=0, now=1000)
        changes = store._connect().total_changes
        second = store.automation_observe(tree_size=1, confirmed_size=0, now=1005)
        self.assertEqual(store._connect().total_changes, changes)
        self.assertEqual(second, first)

    def test_confirmation_path_and_balance_gate_before_signing(self):
        self._leaf()
        self._arm_auto()
        signs = []
        with (
            patch.object(auto_anchor, "_runtime_allowed", return_value=True),
            patch.object(auto_anchor, "_confirmation_path_verified", return_value=False),
        ):
            self.assertIsNone(auto_anchor.tick(now=1000))
            self.assertIsNone(
                auto_anchor.tick(
                    now=1900,
                    sign_fn=lambda ident: signs.append(ident),
                    balance_fetch_fn=lambda _addr: 10_000_000,
                    params_fetch_fn=self._params,
                )
            )
        self.assertEqual(signs, [])
        self.assertIsNone(store.last_automation_job())

        with (
            patch.object(auto_anchor, "_runtime_allowed", return_value=True),
            patch.object(auto_anchor, "_confirmation_path_verified", return_value=True),
        ):
            self.assertIsNone(
                auto_anchor.tick(
                    now=1960,
                    sign_fn=lambda ident: signs.append(ident),
                    balance_fetch_fn=lambda _addr: canary.AUTO_BALANCE_HALT - 1,
                    params_fetch_fn=self._params,
                )
            )
        self.assertEqual(signs, [])
        self.assertIsNone(store.last_automation_job())

    def test_failed_preflight_is_throttled_without_sqlite_writes(self):
        self._leaf()
        self._arm_auto()
        checks = []

        def unavailable(**_kwargs):
            checks.append(True)
            return False

        with (
            patch.object(auto_anchor, "_runtime_allowed", return_value=True),
            patch.object(
                auto_anchor, "_confirmation_path_verified", side_effect=unavailable
            ),
        ):
            self.assertIsNone(auto_anchor.tick(now=1000))
            self.assertIsNone(auto_anchor.tick(now=1900))
            changes = store._connect().total_changes
            self.assertIsNone(auto_anchor.tick(now=1901))
            self.assertEqual(store._connect().total_changes, changes)
            self.assertIsNone(auto_anchor.tick(now=1960))
        self.assertEqual(len(checks), 2)

    def test_budget_precheck_avoids_signing(self):
        self._leaf()
        self._arm_auto()
        signs = []
        with (
            patch.object(auto_anchor, "_runtime_allowed", return_value=True),
            patch.object(auto_anchor, "_confirmation_path_verified", return_value=True),
            patch.object(
                store,
                "automatic_budget_usage",
                return_value={
                    "hourly_count": 0,
                    "daily_fee": canary.AUTO_DAILY_FEE_MAX,
                    "monthly_fee": canary.AUTO_DAILY_FEE_MAX,
                },
            ),
        ):
            self.assertIsNone(auto_anchor.tick(now=1000))
            self.assertIsNone(
                auto_anchor.tick(
                    now=1900,
                    sign_fn=lambda ident: signs.append(ident),
                    balance_fetch_fn=lambda _addr: 10_000_000,
                    params_fetch_fn=self._params,
                )
            )
        self.assertEqual(signs, [])
        self.assertIsNone(store.last_automation_job())

    def test_observation_waits_15_minutes_then_confirms_once(self):
        root = self._leaf()
        self._arm_auto()
        blob = _signed(1, root)
        expected = algo_anchor.signed_txn_txid(blob)
        sends = []
        with (
            patch.object(auto_anchor, "_runtime_allowed", return_value=True),
            patch.object(auto_anchor, "_confirmation_path_verified", return_value=True),
        ):
            first = auto_anchor.tick(
                now=1000,
                sign_fn=lambda _ident: blob,
                send_fn=lambda raw: sends.append(bytes(raw)) or expected,
                fetch_fn=lambda _txid: _indexer(expected, 1, root),
                balance_fetch_fn=lambda _addr: 10_000_000,
                params_fetch_fn=self._params,
            )
            self.assertIsNone(first)
            done = auto_anchor.tick(
                now=1900,
                sign_fn=lambda _ident: blob,
                send_fn=lambda raw: sends.append(bytes(raw)) or expected,
                fetch_fn=lambda _txid: _indexer(expected, 1, root),
                balance_fetch_fn=lambda _addr: 10_000_000,
                params_fetch_fn=self._params,
            )
        self.assertEqual(len(sends), 1)
        self.assertEqual(done["size"], 1)
        self.assertEqual(store.last_confirmed_checkpoint()["txid"], expected)
        self.assertEqual(store.automation_job_at(1)["status"], "CONFIRMED")

    def test_submitted_confirmation_poll_is_throttled_without_second_post(self):
        root = self._leaf()
        self._arm_auto()
        blob = _signed(1, root)
        expected = algo_anchor.signed_txn_txid(blob)
        sends = []
        fetches = []

        def not_confirmed(_txid):
            fetches.append(True)
            return None

        with (
            patch.object(auto_anchor, "_runtime_allowed", return_value=True),
            patch.object(auto_anchor, "_confirmation_path_verified", return_value=True),
        ):
            self.assertIsNone(auto_anchor.tick(now=1000))
            submitted = auto_anchor.tick(
                now=1900,
                sign_fn=lambda _ident: blob,
                send_fn=lambda raw: sends.append(bytes(raw)) or expected,
                fetch_fn=not_confirmed,
                balance_fetch_fn=lambda _addr: 10_000_000,
                params_fetch_fn=self._params,
            )
            self.assertEqual(submitted["send_state"], canary.STATE_SUBMITTED)
            self.assertEqual(len(fetches), 1)
            auto_anchor.tick(now=1905, fetch_fn=not_confirmed)
            self.assertEqual(len(fetches), 1)
            auto_anchor.tick(now=1910, fetch_fn=not_confirmed)
        self.assertEqual(len(sends), 1)
        self.assertEqual(len(fetches), 2)

    def test_atomic_latch_blocks_second_post_after_crash(self):
        root = self._leaf()
        self._arm_auto()
        blob = _signed(1, root)
        params = self._params()
        note = store.checkpoint_at(1)
        job = store.create_automation_job(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=root,
            checkpoint=note,
            request_id="crash-job",
            params=params,
            authorize_at=1000,
        )
        policy = algo_anchor.hmac_policy(
            algo_anchor.fee_policy_snapshot(
                params,
                unsigned=algo_anchor.build_mainnet_payment_txn(
                    algo_anchor.encode_note(ORIGIN_MAINNET, 1, root), params
                ),
                now=1000,
            )
        )
        canary.persist_authorized(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=root,
            checkpoint=note,
            request_id=job["request_id"],
            signed=blob,
            at=1000,
            _capability=canary._FIXTURE_PERSIST_CAPABILITY,
            fee_policy=policy,
            fv=100,
            lv=1100,
        )
        store.set_automation_job_status(1, "AUTHORIZED", now=1000)
        with self.assertRaises(canary.CanaryError):
            canary.send_automatic_durable(
                store.authorized_at(1),
                _capability=canary._AUTO_SEND_CAPABILITY,
                balance_fetch_fn=lambda _addr: 10_000_000,
                now=1000,
                crash_before_post=True,
            )
        self.assertEqual(
            store.authorized_at(1)["send_state"], canary.STATE_SEND_ATTEMPTED
        )
        sent = []
        with self.assertRaises(canary.CanaryError):
            canary.send_automatic_durable(
                store.authorized_at(1),
                _capability=canary._AUTO_SEND_CAPABILITY,
                send_fn=lambda raw: sent.append(raw),
                fetch_fn=lambda _txid: None,
                balance_fetch_fn=lambda _addr: 10_000_000,
                now=1001,
            )
        self.assertEqual(sent, [])

    def test_ambiguous_post_after_resign_stays_latched_for_poll_only(self):
        root = self._leaf()
        self._arm_auto()
        blob = _signed(1, root)
        params = self._params()
        note = store.checkpoint_at(1)
        job = store.create_automation_job(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=root,
            checkpoint=note,
            request_id="stale-before-post",
            params=params,
            authorize_at=1000,
        )
        policy = algo_anchor.hmac_policy(
            algo_anchor.fee_policy_snapshot(
                params,
                unsigned=algo_anchor.build_mainnet_payment_txn(
                    algo_anchor.encode_note(ORIGIN_MAINNET, 1, root), params
                ),
                now=1000,
            )
        )
        canary.persist_authorized(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=root,
            checkpoint=note,
            request_id=job["request_id"],
            signed=blob,
            at=1000,
            _capability=canary._FIXTURE_PERSIST_CAPABILITY,
            fee_policy=policy,
            fv=100,
            lv=1100,
        )
        store.set_automation_job_status(1, "AUTHORIZED", now=1000)
        result = auto_anchor._resume_job(
            store.automation_job_at(1),
            now=1200,
            sign_fn=lambda _ident: blob,
            send_fn=lambda _raw: (_ for _ in ()).throw(TimeoutError("lost response")),
            balance_fetch_fn=lambda _addr: 10_000_000,
            params_fetch_fn=self._params,
        )
        self.assertEqual(result["status"], canary.STATE_SEND_ATTEMPTED)
        self.assertEqual(
            store.authorized_at(1)["send_state"], canary.STATE_SEND_ATTEMPTED
        )
        self.assertEqual(
            store.automation_job_at(1)["status"], canary.STATE_SEND_ATTEMPTED
        )
        self.assertEqual(store.automation_job_at(1)["resign_count"], 1)

    def test_confirmation_commit_gap_repairs_by_exact_txid_without_post(self):
        root = self._leaf()
        self._arm_auto()
        blob = _signed(1, root)
        expected = algo_anchor.signed_txn_txid(blob)
        params = self._params()
        note = store.checkpoint_at(1)
        job = store.create_automation_job(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=root,
            checkpoint=note,
            request_id="confirm-gap",
            params=params,
            authorize_at=1000,
        )
        policy = algo_anchor.hmac_policy(
            algo_anchor.fee_policy_snapshot(
                params,
                unsigned=algo_anchor.build_mainnet_payment_txn(
                    algo_anchor.encode_note(ORIGIN_MAINNET, 1, root), params
                ),
                now=1000,
            )
        )
        canary.persist_authorized(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=root,
            checkpoint=note,
            request_id=job["request_id"],
            signed=blob,
            at=1000,
            _capability=canary._FIXTURE_PERSIST_CAPABILITY,
            fee_policy=policy,
            fv=100,
            lv=1100,
        )
        store.set_automation_job_status(1, "AUTHORIZED", now=1000)
        with (
            patch.object(
                store, "save_confirmed_checkpoint", side_effect=OSError("crash gap")
            ),
            self.assertRaises(OSError),
        ):
            canary.send_automatic_durable(
                store.authorized_at(1),
                _capability=canary._AUTO_SEND_CAPABILITY,
                send_fn=lambda _raw: expected,
                fetch_fn=lambda _txid: _indexer(expected, 1, root),
                balance_fetch_fn=lambda _addr: 10_000_000,
                now=1000,
            )
        self.assertEqual(store.authorized_at(1)["send_state"], "CONFIRMED")
        self.assertEqual(store.last_confirmed_checkpoint()["size"], 0)
        with patch.object(auto_anchor, "_budget_block", return_value="daily_fee_cap"):
            repaired = auto_anchor._resume_job(
                store.automation_job_at(1),
                now=1001,
                fetch_fn=lambda _txid: _indexer(expected, 1, root),
            )
        self.assertEqual(repaired["txid"], expected)
        self.assertEqual(store.last_confirmed_checkpoint()["size"], 1)
        self.assertEqual(store.automation_job_at(1)["status"], "CONFIRMED")

    def test_authorized_provider_failure_is_throttled_without_state_loss(self):
        root = self._leaf()
        self._arm_auto()
        blob = _signed(1, root)
        params = self._params()
        note = store.checkpoint_at(1)
        job = store.create_automation_job(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=root,
            checkpoint=note,
            request_id="provider-wait",
            params=params,
            authorize_at=1000,
        )
        policy = algo_anchor.hmac_policy(
            algo_anchor.fee_policy_snapshot(
                params,
                unsigned=algo_anchor.build_mainnet_payment_txn(
                    algo_anchor.encode_note(ORIGIN_MAINNET, 1, root), params
                ),
                now=1000,
            )
        )
        canary.persist_authorized(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=root,
            checkpoint=note,
            request_id=job["request_id"],
            signed=blob,
            at=1000,
            _capability=canary._FIXTURE_PERSIST_CAPABILITY,
            fee_policy=policy,
            fv=100,
            lv=1100,
        )
        store.set_automation_job_status(1, "AUTHORIZED", now=1000)
        attempts = []

        def unavailable(_addr):
            attempts.append(True)
            raise algo_anchor.AnchorError("balance unavailable")

        with patch.object(auto_anchor, "_runtime_allowed", return_value=True):
            first = auto_anchor.tick(now=1000, balance_fetch_fn=unavailable)
            second = auto_anchor.tick(now=1005, balance_fetch_fn=unavailable)
            third = auto_anchor.tick(now=1060, balance_fetch_fn=unavailable)
        self.assertEqual(first["status"], "PRE_POST_WAIT")
        self.assertEqual(second["status"], "AUTHORIZED")
        self.assertEqual(third["status"], "PRE_POST_WAIT")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(store.automation_job_at(1)["status"], "AUTHORIZED")
        self.assertEqual(store.authorized_at(1)["send_state"], "AUTHORIZED")

    def test_budget_reservation_is_bounded_and_race_safe(self):
        root = self._leaf()
        blob = _signed(1, root)
        note = store.checkpoint_at(1)
        params = self._params()
        job = store.create_automation_job(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=root,
            checkpoint=note,
            request_id="budget",
            params=params,
            authorize_at=1000,
        )
        store.save_authorized_checkpoint(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=root,
            checkpoint=note,
            request_id=job["request_id"],
            signed=blob,
            at=1000,
            send_state="AUTHORIZED",
            fee_policy={"canonical_fee": 3000},
            fv=100,
            lv=1100,
        )
        store.set_automation_job_status(1, "AUTHORIZED", now=1000)
        txid = algo_anchor.signed_txn_txid(blob)
        with self.assertRaises(store.StoreError):
            store.reserve_automatic_send(
                tree_size=1,
                expected_txid=txid,
                fee=3000,
                now=1000,
                hour_start=0,
                day_start=0,
                month_start=0,
                hourly_max=12,
                daily_max=2999,
                monthly_max=10_000_000,
            )
        self.assertEqual(store.authorized_at(1)["send_state"], "AUTHORIZED")
        self.assertEqual(store.automation_job_at(1)["status"], "AUTHORIZED")
        self.assertEqual(
            store.automatic_budget_usage(
                hour_start=0, day_start=0, month_start=0
            ),
            {"hourly_count": 0, "daily_fee": 0, "monthly_fee": 0},
        )
        store.reserve_automatic_send(
            tree_size=1,
            expected_txid=txid,
            fee=3000,
            now=1000,
            hour_start=0,
            day_start=0,
            month_start=0,
            hourly_max=12,
            daily_max=500_000,
            monthly_max=10_000_000,
        )
        with self.assertRaises(store.StoreError):
            store.reserve_automatic_send(
                tree_size=1,
                expected_txid=txid,
                fee=3000,
                now=1000,
                hour_start=0,
                day_start=0,
                month_start=0,
                hourly_max=12,
                daily_max=500_000,
                monthly_max=10_000_000,
            )
        self.assertEqual(
            store.automatic_budget_usage(
                hour_start=0, day_start=0, month_start=0
            ),
            {"hourly_count": 1, "daily_fee": 3000, "monthly_fee": 3000},
        )

    def test_monitor_status_omits_frozen_and_request_material(self):
        root = self._leaf()
        job = store.create_automation_job(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=root,
            checkpoint=store.checkpoint_at(1),
            request_id="must-not-be-public",
            params=self._params(),
            authorize_at=1000,
        )
        self.assertTrue(job["checkpoint"])
        status = auto_anchor.status(now=1000)
        public_job = status["last_job"]
        for field in (
            "checkpoint",
            "params",
            "request_id",
            "root",
            "superseded_signed_sha256",
        ):
            self.assertNotIn(field, public_job)


if __name__ == "__main__":
    unittest.main()
