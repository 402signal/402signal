"""Operator snapshot fields. No secrets. No live network."""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import payment, ready
from live402.pq import algo_anchor, monitor, ops_state, store, worker
from tests.pq_test_env import clear_pq_env


class MonitorSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clear_pq_env()
        ops_state.reset()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        worker.clear_queue()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        worker.clear_queue()
        clear_pq_env()
        ops_state.reset()
        store.reset()
        self.tmp.cleanup()

    def test_snapshot_has_operator_fields_no_secrets(self):
        store.append(b"leaf")
        snap = monitor.snapshot()
        for key in (
            "epoch",
            "network",
            "tree_size",
            "last_authorized",
            "last_submitted",
            "last_confirmed",
            "gaps",
            "ages",
            "errors",
            "signer",
            "submit_provider",
            "confirm_provider",
            "fee",
        ):
            self.assertIn(key, snap)
        self.assertEqual(snap["tree_size"], 1)
        self.assertEqual(snap["network"], "testnet")
        self.assertIn("authorized_s", snap["ages"])
        self.assertIn("available", snap["signer"])
        self.assertIn("max_fee", snap["fee"])
        self.assertEqual(snap["fee"]["max_fee"], 30000)
        self.assertIn("last_error", snap["errors"])
        self.assertIn("db_errors", snap["errors"])
        self.assertIn("recovery_conflicts", snap["errors"])
        self.assertIn("org", snap["submit_provider"])
        self.assertIn("org", snap["confirm_provider"])
        blob = str(snap).lower()
        self.assertNotIn("mnemonic", blob)
        self.assertNotIn("private_key", blob)
        self.assertNotIn("live402_pq_log_sk", blob)
        self.assertNotIn("hmac", blob)

    def test_ready_uses_boolean_subset_not_snapshot(self):
        flags = monitor.ready_flags()
        self.assertIn("pq_log_sqlite", flags)
        self.assertIn("pq_log_integrity", flags)
        payload = ready.readiness()
        self.assertIn("ok", payload)
        self.assertEqual(
            set(payload["checks"]),
            {"storage", "catalog", "history", "pq_log", "replay_ledger"},
        )
        self.assertNotIn("last_authorized", payload)
        self.assertNotIn("submit_provider", payload)
        self.assertNotIn("last_error", payload)

    def test_non_pq1_alert_is_incident(self):
        ids = {row["id"]: row for row in monitor.ALERTS}
        self.assertEqual(ids["unexpected_non_pq1_txn"]["severity"], "incident")
        self.assertIn("recovery_conflict", ids)
        decoded = {
            "pq_auth": b"",
            "sender": payment.DEFAULT_PAYTO_ALGORAND,
            "receiver": payment.DEFAULT_PAYTO_ALGORAND,
            "amount": 5,
            "fee": 3000,
            "tx_type": "axfer",
            "note": b"",
            "has_axfer": True,
        }
        out = algo_anchor.classify_falcon_account_txn(
            decoded, expected_address=payment.DEFAULT_PAYTO_ALGORAND
        )
        self.assertTrue(out["incident"])
        snap = monitor.snapshot()
        self.assertGreaterEqual(snap["errors"]["non_pq1_incidents"], 1)


if __name__ == "__main__":
    unittest.main()
