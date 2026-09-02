"""PRODUCTION PQ identity is MainNet-only. Unset/unknown fail closed.

No live network. No ceremony. No DB reset. Secrets are named, not valued.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import payment
from live402.pq import ORIGIN, ORIGIN_MAINNET, algo_anchor, store, worker
from live402.pq import log_identity
from live402.pq import transparency as pq_view
from live402.server import boot_http_process
from tests.pq_test_env import clear_pq_env


_TXID = "B" * 52
_MAINNET_ADDR = "GVIAG3YMJ7OLJ3JAUBNI2YP5JCQQCQYWN25UAGLC2BTPOBUL3ZZTILIMWU"
_TESTNET_ADDR = "OBHYXCUVOLSTZVBN5JUFIYBD4X4ZFIAFZMWMU2P45VBYGWT26MV34IFFIU"


class ProductionFailClosedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.testnet_db = os.path.join(self.tmp.name, "pq-log.sqlite")
        self.mainnet_db = os.path.join(self.tmp.name, "pq-log-mainnet.sqlite")
        self._saved_fixture = os.environ.get("LIVE402_FIXTURE")
        clear_pq_env()
        os.environ.pop("LIVE402_FIXTURE", None)
        os.environ.pop("LIVE402_PQ_TEST_SUPPORT", None)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        worker.clear_queue()
        clear_pq_env()
        if self._saved_fixture is None:
            os.environ.pop("LIVE402_FIXTURE", None)
        else:
            os.environ["LIVE402_FIXTURE"] = self._saved_fixture
        store.reset()
        self.tmp.cleanup()

    def _arm_production(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_LOG_DB"] = self.mainnet_db
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN_MAINNET
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = _MAINNET_ADDR
        os.environ["LIVE402_PQ_SIGNER_MAINNET_TOKEN"] = "named-not-valued"
        store.reset()

    def test_unset_network_fails_closed(self):
        os.environ.pop("LIVE402_PQ_FALCON_NETWORK", None)
        with self.assertRaises(log_identity.ConfigError) as ctx:
            log_identity.configured_network()
        self.assertIn("unset", str(ctx.exception).lower())
        with self.assertRaises(log_identity.ConfigError):
            log_identity.live_network_name()
        self.assertTrue(worker._production_or_mainnet())
        self.assertIsNone(worker.maybe_submit(None))
        self.assertIsNone(worker.maybe_confirm())
        self.assertIsNone(worker.tick())
        self.assertFalse(algo_anchor.submit_allowed())
        self.assertFalse(algo_anchor.mainnet_submit_allowed())
        self.assertIsNone(algo_anchor.send_if_allowed(b"STXN", send_fn=lambda _b: _TXID))

    def test_unknown_network_fails_closed(self):
        for bad in ("prod", "mainnett", "MAIN", "dev"):
            os.environ["LIVE402_PQ_FALCON_NETWORK"] = bad
            with self.assertRaises(log_identity.ConfigError) as ctx:
                log_identity.configured_network()
            self.assertIn("unknown", str(ctx.exception).lower())

    def test_unset_epoch_fails_closed_in_production(self):
        os.environ.pop("LIVE402_PQ_LOG_EPOCH", None)
        with self.assertRaises(log_identity.ConfigError) as ctx:
            log_identity.configured_epoch()
        self.assertIn("unset", str(ctx.exception).lower())

    def test_production_rejects_testnet_network(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "testnet-v1"
        os.environ["LIVE402_PQ_LOG_DB"] = self.testnet_db
        with self.assertRaises(log_identity.ConfigError):
            log_identity.require_production_boot()
        with self.assertRaises(log_identity.ConfigError):
            log_identity.resolve_db_path(self.testnet_db)

    def test_production_rejects_testnet_db_and_origin(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_LOG_DB"] = self.testnet_db
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN
        with self.assertRaises(log_identity.ConfigError):
            log_identity.resolve_db_path(self.testnet_db)
        os.environ["LIVE402_PQ_LOG_DB"] = self.mainnet_db
        with self.assertRaises(log_identity.ConfigError):
            log_identity.configured_origin()

    def test_production_boot_requires_mainnet_identity(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_LOG_DB"] = self.mainnet_db
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN_MAINNET
        with self.assertRaises(log_identity.ConfigError):
            log_identity.require_production_boot()
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = _MAINNET_ADDR
        log_identity.require_production_boot()
        boot_http_process()

    def test_production_never_falls_back_to_testnet_signer_or_sk(self):
        self._arm_production()
        os.environ.pop("LIVE402_PQ_SIGNER_MAINNET_TOKEN", None)
        os.environ["LIVE402_PQ_SIGNER_TOKEN"] = "must-not-be-used"
        os.environ["LIVE402_PQ_LOG_SK"] = "aa" * 32
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = _TESTNET_ADDR
        self.assertTrue(worker._production_or_mainnet())
        self.assertIsNone(worker.maybe_submit(None, now=15 * 60))
        self.assertIsNone(worker.tick())
        self.assertFalse(algo_anchor.signer_material_present())
        self.assertEqual(algo_anchor.falcon_address(), _MAINNET_ADDR)
        self.assertNotEqual(algo_anchor.falcon_address(), _TESTNET_ADDR)
        from live402.pq import signer_client

        self.assertTrue(signer_client.token_configured())
        self.assertFalse(algo_anchor.submit_allowed())
        self.assertIsNone(worker.maybe_confirm())
        with self.assertRaises(algo_anchor.AnchorError):
            worker.confirm_testnet_anchor(_TXID)
        os.environ["LIVE402_PQ_SIGNER_MAINNET_TOKEN"] = "named-not-valued"
        self.assertTrue(algo_anchor.signer_material_present())
        self.assertIsNone(worker.maybe_submit(None, now=15 * 60))
        self.assertFalse(algo_anchor.submit_allowed())

    def test_production_tick_boot_never_auto_send(self):
        self._arm_production()
        os.environ["LIVE402_PQ_FALCON_MAINNET_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        sent = []
        store.append(b"one")
        out = worker.tick(
            sender=_MAINNET_ADDR,
            params={
                "genesisID": algo_anchor.MAINNET_GENESIS_ID,
                "genesisHash": algo_anchor.MAINNET_GENESIS_HASH,
            },
            now=15 * 60,
            send_fn=lambda blob: sent.append(blob) or _TXID,
        )
        self.assertIsNone(out)
        self.assertEqual(sent, [])
        self.assertFalse(algo_anchor.automatic_mainnet_enabled())
        src = Path(worker.__file__).read_text(encoding="utf-8")
        self.assertNotIn("submit_mainnet_canary", src)
        self.assertIn("Automatic MainNet is off", src)

    def test_authorized_send_attempted_submitted_never_confirmed(self):
        os.environ["LIVE402_FIXTURE"] = "1"
        os.environ["LIVE402_PQ_LOG_DB"] = self.testnet_db
        store.reset()
        store.append(b"one")
        root = store.root(1)
        from tests.pq_test_env import insert_authorized_fixture

        insert_authorized_fixture(
            tree_size=1,
            origin=ORIGIN,
            root=root,
            checkpoint="",
            signed=b"STXN",
            send_state="SEND_ATTEMPTED",
            submitted=False,
            txid=_TXID,
        )
        life = pq_view.authorized_lifecycle()
        self.assertIsNotNone(life)
        self.assertEqual(life["status"], "SEND_ATTEMPTED")
        self.assertNotEqual(life["status"], "CONFIRMED")
        self.assertNotIn("CONFIRMED", life["label"])
        html = pq_view.page_html()
        self.assertIn("SEND_ATTEMPTED", html)
        self.assertNotIn(">CONFIRMED<", html)
        self.assertIsNone(worker.public_anchor())

        insert_authorized_fixture(
            tree_size=1,
            origin=ORIGIN,
            root=root,
            checkpoint="",
            signed=b"STXN",
            send_state="SUBMITTED",
            submitted=True,
            txid=_TXID,
        )
        life = pq_view.authorized_lifecycle()
        self.assertEqual(life["status"], "SUBMITTED")
        self.assertNotEqual(life["status"], "CONFIRMED")
        self.assertIsNone(worker.public_anchor())

        insert_authorized_fixture(
            tree_size=1,
            origin=ORIGIN,
            root=root,
            checkpoint="",
            signed=b"STXN",
            send_state="CONFIRMED",
            submitted=True,
            txid=_TXID,
        )
        life = pq_view.authorized_lifecycle()
        self.assertNotEqual(life["status"], "CONFIRMED")
        self.assertIn(life["status"], {"AUTHORIZED", "SEND_ATTEMPTED", "SUBMITTED"})

    def test_mainnet_evidence_never_testnet_explorer(self):
        self._arm_production()
        store.append(b"one")
        root = store.root(1)
        store.save_confirmed_checkpoint(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=root,
            txid=_TXID,
            confirmed_round=99,
            at=1_700_000_000,
            network="mainnet",
            genesis_id=algo_anchor.MAINNET_GENESIS_ID,
        )
        conf = worker.public_anchor()
        self.assertIsNotNone(conf)
        self.assertIn(algo_anchor.MAINNET_EXPLORER_TX_URL, conf["explorer"])
        self.assertNotIn("testnet.explorer.perawallet.app", conf["explorer"])
        view = pq_view.confirmed_view()
        self.assertEqual(view["network"], "mainnet")
        self.assertIn(algo_anchor.MAINNET_EXPLORER_TX_URL, view["explorer"])
        self.assertNotIn("testnet.explorer.perawallet.app", view["explorer"])
        self.assertIn(algo_anchor.MAINNET_INDEXER_TXN_URL, view["indexer"])
        html = pq_view.page_html()
        self.assertNotIn("testnet.explorer.perawallet.app", html)
        self.assertNotIn("testnet-idx.algonode.cloud", html)
        self.assertIn(algo_anchor.MAINNET_EXPLORER_TX_URL + _TXID, html)

    def test_missing_recorded_network_suppresses_production_explorer(self):
        self._arm_production()
        store.append(b"one")
        root = store.root(1)
        store.save_confirmed_checkpoint(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=root,
            txid=_TXID,
            confirmed_round=99,
            at=1_700_000_000,
        )
        conf = worker.public_anchor()
        self.assertIsNotNone(conf)
        self.assertEqual(conf.get("explorer") or "", "")
        view = pq_view.confirmed_view()
        self.assertEqual(view.get("explorer") or "", "")
        self.assertNotIn("testnet.explorer.perawallet.app", pq_view.page_html())

    def test_fly_toml_has_no_retired_testnet_production_deps(self):
        text = Path(__file__).resolve().parent.parent.joinpath("fly.toml").read_text(encoding="utf-8")
        self.assertIn('LIVE402_PQ_FALCON_NETWORK = "mainnet"', text)
        self.assertIn('LIVE402_PQ_LOG_EPOCH = "mainnet-v1"', text)
        self.assertIn('LIVE402_PQ_LOG_DB = "/data/pq-log-mainnet.sqlite"', text)
        self.assertIn('LIVE402_PQ_LOG_ORIGIN = "402signal.com/pq/log/mainnet-v1"', text)
        self.assertIn(_MAINNET_ADDR, text)
        self.assertNotIn("LIVE402_PQ_FALCON_BROADCAST", text)
        self.assertNotIn("LIVE402_PQ_SIGNER_TOKEN", text)
        self.assertNotIn("LIVE402_PQ_LOG_SK ", text)
        self.assertNotIn("LIVE402_PQ_FALCON_ADDRESS =", text)
        self.assertNotIn(_TESTNET_ADDR, text)
        self.assertIn("MainNet-only", text)

    def test_retired_env_names_are_documented_not_production(self):
        retired = log_identity.production_retired_testnet_envs()
        self.assertIn("LIVE402_PQ_LOG_SK", retired)
        self.assertIn("LIVE402_PQ_SIGNER_TOKEN", retired)
        self.assertIn("LIVE402_PQ_FALCON_BROADCAST", retired)
        self.assertIn("LIVE402_PQ_FALCON_ADDRESS", retired)
        self.assertNotIn("LIVE402_PQ_SIGNER_MAINNET_TOKEN", retired)
        self.assertNotIn("LIVE402_PQ_LOG_SK_MAINNET", retired)


class TestSupportIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.setdefault("LIVE402_FIXTURE", "1")
        clear_pq_env()
        os.environ["LIVE402_FIXTURE"] = "1"
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        clear_pq_env()
        os.environ["LIVE402_FIXTURE"] = "1"
        store.reset()
        self.tmp.cleanup()

    def test_fixture_may_omit_network_for_test_support_only(self):
        self.assertTrue(log_identity.is_test_support())
        self.assertFalse(log_identity.is_production_runtime())
        self.assertEqual(log_identity.configured_network(), "")
        self.assertEqual(log_identity.live_network_name(), "testnet")
        self.assertEqual(log_identity.configured_epoch(), "testnet-v1")


if __name__ == "__main__":
    unittest.main()
