"""MainNet fail-closed gates. No live network. No MainNet transaction."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import payment
from live402.pq import ORIGIN, ORIGIN_MAINNET, algo_anchor, store, worker
from live402.pq import log_identity
from live402.pq import monitor
from live402.pq import network as netcfg
from live402.pq import trust
from tests.pq_test_env import MAINNET_ENV_KEYS, clear_pq_env


_TXID = "B" * 52


class MainNetGatingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.testnet_db = os.path.join(self.tmp.name, "pq-log.sqlite")
        self.mainnet_db = os.path.join(self.tmp.name, "pq-log-mainnet.sqlite")
        clear_pq_env()
        os.environ["LIVE402_PQ_LOG_DB"] = self.testnet_db
        store.reset()
        worker.clear_queue()
        self._env_keys = MAINNET_ENV_KEYS
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        worker.clear_queue()
        for key in self._env_keys:
            os.environ.pop(key, None)
        store.reset()
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        self.tmp.cleanup()

    def _arm_mainnet_identity(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_LOG_DB"] = self.mainnet_db
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN_MAINNET
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = payment.DEFAULT_PAYTO_ALGORAND
        os.environ["LIVE402_PQ_SIGNER_MAINNET_TOKEN"] = "named-not-valued"
        store.reset()

    def test_typo_epoch_and_network_error(self):
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnett"
        with self.assertRaises(log_identity.ConfigError):
            log_identity.configured_epoch()
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet"
        with self.assertRaises(log_identity.ConfigError):
            log_identity.configured_epoch()
        os.environ.pop("LIVE402_PQ_LOG_EPOCH", None)
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnett"
        with self.assertRaises(log_identity.ConfigError):
            log_identity.configured_network()
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "prod"
        with self.assertRaises(log_identity.ConfigError):
            log_identity.configured_network()

    def test_testnet_broadcast_plus_network_mainnet_never_sends(self):
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ.pop("LIVE402_PQ_FALCON_MAINNET_BROADCAST", None)
        sent = []
        params = {
            "genesisID": algo_anchor.MAINNET_GENESIS_ID,
            "genesisHash": algo_anchor.MAINNET_GENESIS_HASH,
        }
        self.assertIsNone(
            algo_anchor.send_if_allowed(
                b"STXN",
                send_fn=lambda blob: sent.append(blob) or _TXID,
                params=params,
            )
        )
        self.assertFalse(algo_anchor.submit_allowed(params=params))
        self.assertFalse(algo_anchor.mainnet_submit_allowed(params=params))
        self.assertEqual(sent, [])

    def test_mainnet_flag_plus_network_testnet_never_mainnet_sends(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        os.environ["LIVE402_PQ_FALCON_MAINNET_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = payment.DEFAULT_PAYTO_ALGORAND
        sent = []
        params = {
            "genesisID": algo_anchor.MAINNET_GENESIS_ID,
            "genesisHash": algo_anchor.MAINNET_GENESIS_HASH,
        }
        self.assertIsNone(
            algo_anchor.send_if_allowed(
                b"STXN",
                send_fn=lambda blob: sent.append(blob) or _TXID,
                params=params,
            )
        )
        self.assertFalse(algo_anchor.mainnet_submit_allowed(params=params))
        self.assertEqual(sent, [])

    def test_network_mainnet_requires_full_identity(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_LOG_DB"] = self.testnet_db
        with self.assertRaises(log_identity.ConfigError):
            log_identity.require_mainnet_identity(
                db_path=self.testnet_db,
                origin=ORIGIN,
            )
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "testnet-v1"
        with self.assertRaises(log_identity.ConfigError):
            log_identity.require_mainnet_identity(
                db_path=self.mainnet_db,
                origin=ORIGIN_MAINNET,
            )
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        with self.assertRaises(log_identity.ConfigError):
            log_identity.require_mainnet_identity(
                db_path=self.mainnet_db,
                origin=ORIGIN,
            )
        with self.assertRaises(log_identity.ConfigError):
            log_identity.require_mainnet_identity(
                db_path=self.mainnet_db,
                origin=ORIGIN_MAINNET,
            )
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = payment.DEFAULT_PAYTO_ALGORAND
        with self.assertRaises(log_identity.ConfigError) as ctx:
            log_identity.require_mainnet_identity(
                db_path=self.mainnet_db,
                origin=ORIGIN_MAINNET,
            )
        self.assertIn("signer", str(ctx.exception).lower())
        os.environ.pop("LIVE402_PQ_FALCON_MAINNET_ADDRESS", None)
        os.environ["LIVE402_PQ_SIGNER_MAINNET_TOKEN"] = "named-not-valued"
        with self.assertRaises(log_identity.ConfigError) as ctx:
            log_identity.require_mainnet_identity(
                db_path=self.mainnet_db,
                origin=ORIGIN_MAINNET,
            )
        self.assertIn("address", str(ctx.exception).lower())
        self._arm_mainnet_identity()
        log_identity.require_mainnet_identity(
            db_path=self.mainnet_db,
            origin=ORIGIN_MAINNET,
        )

    def test_network_mainnet_cannot_operate_against_testnet_log(self):
        """NETWORK=mainnet must not open the TestNet DB or origin."""
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_LOG_DB"] = self.testnet_db
        with self.assertRaises(log_identity.ConfigError):
            store.db_path()
        with self.assertRaises(log_identity.ConfigError):
            log_identity.resolve_db_path(self.testnet_db)
        with self.assertRaises(log_identity.ConfigError):
            log_identity.configured_origin()
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN
        with self.assertRaises(log_identity.ConfigError):
            log_identity.configured_origin()
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN_MAINNET
        with self.assertRaises(log_identity.ConfigError):
            store.db_path()
        os.environ["LIVE402_PQ_LOG_DB"] = self.mainnet_db
        self.assertEqual(log_identity.configured_origin(), ORIGIN_MAINNET)
        self.assertEqual(store.db_path(), self.mainnet_db)
        store.reset()
        self.assertEqual(store.origin(), ORIGIN_MAINNET)
        self.assertNotEqual(store.origin(), ORIGIN)

    def test_mainnet_cannot_use_testnet_database_path(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_LOG_DB"] = self.testnet_db
        with self.assertRaises(log_identity.ConfigError):
            store.db_path()
        with self.assertRaises(log_identity.ConfigError):
            log_identity.resolve_db_path(self.testnet_db)

    def test_mainnet_cannot_use_testnet_origin(self):
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN
        with self.assertRaises(log_identity.ConfigError):
            log_identity.configured_origin()

    def test_worker_never_sends_mainnet(self):
        self._arm_mainnet_identity()
        os.environ["LIVE402_PQ_FALCON_MAINNET_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        store.append(b"one")
        sent = []
        out = worker.maybe_submit(
            None,
            payment.DEFAULT_PAYTO_ALGORAND,
            {
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

    def test_canary_not_executed_and_fixture_never_sends(self):
        self._arm_mainnet_identity()
        os.environ["LIVE402_PQ_FALCON_MAINNET_BROADCAST"] = "1"
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.submit_mainnet_canary(
                b"STXN",
                authorize_human_canary=False,
                expected_origin=ORIGIN_MAINNET,
                expected_size=1,
                expected_root=b"\x11" * 32,
            )
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            algo_anchor.submit_mainnet_canary(
                b"STXN",
                authorize_human_canary=True,
                params={
                    "genesisID": algo_anchor.MAINNET_GENESIS_ID,
                    "genesisHash": algo_anchor.MAINNET_GENESIS_HASH,
                },
                expected_origin=ORIGIN_MAINNET,
                expected_size=1,
                expected_root=b"\x11" * 32,
            )
        self.assertIn("canary gate off", str(ctx.exception).lower())
        os.environ["LIVE402_PQ_FALCON_MAINNET_CANARY"] = "1"
        with self.assertRaises(algo_anchor.AnchorError) as ctx2:
            algo_anchor.submit_mainnet_canary(
                b"STXN",
                authorize_human_canary=True,
                params={
                    "genesisID": algo_anchor.MAINNET_GENESIS_ID,
                    "genesisHash": algo_anchor.MAINNET_GENESIS_HASH,
                },
                expected_origin=ORIGIN_MAINNET,
                expected_size=1,
                expected_root=b"\x11" * 32,
            )
        self.assertTrue(
            "fixture" in str(ctx2.exception).lower()
            or "gates failed" in str(ctx2.exception).lower()
        )

    def test_fee_cap_fail_closed(self):
        note = algo_anchor.encode_note(ORIGIN_MAINNET, 1, b"\x11" * 32)
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = payment.DEFAULT_PAYTO_ALGORAND
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            algo_anchor.build_mainnet_payment_txn(
                note,
                {
                    "minFee": 30001,
                    "genesisID": algo_anchor.MAINNET_GENESIS_ID,
                    "genesisHash": algo_anchor.MAINNET_GENESIS_HASH,
                },
            )
        self.assertIn("cap", str(ctx.exception).lower())

    def test_confirm_not_claimed_independent(self):
        confirm = algo_anchor.confirm_provider("mainnet")
        submit = algo_anchor.submit_provider("mainnet")
        self.assertIn("algonode", confirm["host"])
        self.assertIn("algonode", submit["host"])
        self.assertEqual(confirm["org"], "nodely")
        self.assertEqual(submit["org"], "nodely")
        self.assertFalse(confirm["independent_of_submit"])
        self.assertEqual(confirm["independence_status"], "not_met_same_org_nodely")
        self.assertFalse(netcfg.CONFIRM_INDEPENDENT_OF_SUBMIT)
        self.assertFalse(
            netcfg.confirmation_independent(submit["host"], confirm["host"])
        )
        self.assertTrue(netcfg.confirm_host_allowlisted("mainnet", netcfg.NODELY_MAINNET_CONFIRM_HOST))
        self.assertEqual(netcfg.provider_org(netcfg.NODELY_MAINNET_CONFIRM_HOST), "nodely")
        self.assertFalse(
            netcfg.confirmation_independent(submit["host"], netcfg.NODELY_MAINNET_CONFIRM_HOST)
        )
        self.assertFalse(netcfg.confirm_host_allowlisted("mainnet", submit["host"]))
        snap = monitor.snapshot()
        self.assertFalse(snap["confirm_provider"]["independent_of_submit"])
        self.assertEqual(snap["confirm_provider"]["org"], "nodely")
        self.assertEqual(snap["submit_provider"]["org"], "nodely")
        v2 = trust.trust_root_v2()
        self.assertFalse(v2["confirmation_policy"]["independent_provider"])
        self.assertTrue(v2["confirmation_policy"]["same_trust_domain_not_sufficient"])
        self.assertTrue(v2["confirmation_policy"]["algonode_and_nodely_same_org"])
        self.assertEqual(v2["confirmation_policy"]["require"], "fetch_and_decode_actual_txn")

    def test_validate_signed_txn_expected_network(self):
        from live402 import algo_tx
        import base64

        addr = payment.DEFAULT_PAYTO_ALGORAND
        root = b"\x11" * 32
        note = algo_anchor.encode_note(ORIGIN, 1, root)
        gh = base64.b64decode(algo_anchor.TESTNET_GENESIS_HASH)
        txn = algo_tx.pay_txn(addr, addr, 0, 3000, 1, 1001, algo_anchor.TESTNET_GENESIS_ID, gh, note=note)
        blob = algo_tx.msgpack_encode(
            {"pqsig": {"pk": b"pk" + bytes(range(14)), "sch": "f1", "sig": b"sig" + bytes(range(29)), "slt": 0}, "txn": txn}
        )
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = addr
        algo_anchor.validate_signed_txn(
            blob,
            expected_origin=ORIGIN,
            expected_size=1,
            expected_root=root,
            expected_address=addr,
            expected_network="testnet",
        )
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_signed_txn(
                blob,
                expected_origin=ORIGIN,
                expected_size=1,
                expected_root=root,
                expected_address=addr,
                expected_network="mainnet",
            )
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_signed_txn(
                blob,
                expected_origin=ORIGIN,
                expected_size=1,
                expected_root=root,
                expected_address=addr,
                expected_network="mainnett",
            )

    def _mainnet_signed(self, origin=ORIGIN_MAINNET, size=1, root=None):
        from live402 import algo_tx
        import base64

        addr = payment.DEFAULT_PAYTO_ALGORAND
        root = root or (b"\x11" * 32)
        note = algo_anchor.encode_note(origin, size, root)
        gh = base64.b64decode(algo_anchor.MAINNET_GENESIS_HASH)
        txn = algo_tx.pay_txn(
            addr, addr, 0, 3000, 1, 1001, algo_anchor.MAINNET_GENESIS_ID, gh, note=note
        )
        blob = algo_tx.msgpack_encode(
            {
                "pqsig": {
                    "pk": b"pk" + bytes(range(14)),
                    "sch": "f1",
                    "sig": b"sig" + bytes(range(29)),
                    "slt": 0,
                },
                "txn": txn,
            }
        )
        return blob, root, addr

    def test_kill_switch_unset_stops_submit_routing_continues(self):
        self._arm_mainnet_identity()
        os.environ.pop("LIVE402_PQ_FALCON_MAINNET_BROADCAST", None)
        os.environ.pop("LIVE402_PQ_FALCON_MAINNET_CANARY", None)
        blob, root, addr = self._mainnet_signed()
        params = {
            "genesisID": algo_anchor.MAINNET_GENESIS_ID,
            "genesisHash": algo_anchor.MAINNET_GENESIS_HASH,
        }
        sent = []
        self.assertFalse(algo_anchor.mainnet_submit_allowed(params=params, sender=addr))
        self.assertIsNone(
            algo_anchor.send_if_allowed(
                blob,
                send_fn=lambda b: sent.append(b) or _TXID,
                params=params,
            )
        )
        with self.assertRaises(algo_anchor.AnchorError) as ctx:
            algo_anchor.submit_mainnet_canary(
                blob,
                authorize_human_canary=True,
                sender=addr,
                params=params,
                expected_origin=ORIGIN_MAINNET,
                expected_size=1,
                expected_root=root,
                send_fn=lambda b: sent.append(b) or _TXID,
            )
        self.assertIn("canary gate off", str(ctx.exception).lower())
        self.assertEqual(sent, [])
        body = payment.payment_required("https://402signal.com/route")
        self.assertTrue(body.get("accepts"))
        self.assertIn("x402Version", body)

    def test_canary_send_fn_requires_both_flags(self):
        self._arm_mainnet_identity()
        blob, root, addr = self._mainnet_signed()
        params = {
            "genesisID": algo_anchor.MAINNET_GENESIS_ID,
            "genesisHash": algo_anchor.MAINNET_GENESIS_HASH,
            "lastRound": 1,
            "minFee": 1000,
            "fee": 0,
        }
        store.append(b"canary-leaf")
        store.save_authorized_checkpoint(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=root,
            checkpoint="",
            request_id="canary-1",
            signed=blob,
            at=1,
        )
        sent = []

        def send_fn(raw):
            sent.append(bytes(raw))
            return _TXID

        os.environ["LIVE402_PQ_FALCON_MAINNET_BROADCAST"] = "1"
        os.environ.pop("LIVE402_PQ_FALCON_MAINNET_CANARY", None)
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.submit_mainnet_canary(
                blob,
                authorize_human_canary=True,
                sender=addr,
                params=params,
                expected_origin=ORIGIN_MAINNET,
                expected_size=1,
                expected_root=root,
                send_fn=send_fn,
            )
        self.assertEqual(sent, [])
        os.environ.pop("LIVE402_PQ_FALCON_MAINNET_BROADCAST", None)
        os.environ["LIVE402_PQ_FALCON_MAINNET_CANARY"] = "1"
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.submit_mainnet_canary(
                blob,
                authorize_human_canary=True,
                sender=addr,
                params=params,
                expected_origin=ORIGIN_MAINNET,
                expected_size=1,
                expected_root=root,
                send_fn=send_fn,
            )
        self.assertEqual(sent, [])
        os.environ["LIVE402_PQ_FALCON_MAINNET_BROADCAST"] = "1"
        out = algo_anchor.submit_mainnet_canary(
            blob,
            authorize_human_canary=True,
            sender=addr,
            params=params,
            expected_origin=ORIGIN_MAINNET,
            expected_size=1,
            expected_root=root,
            send_fn=send_fn,
        )
        self.assertEqual(out, _TXID)
        self.assertEqual(sent, [blob])
        self.assertFalse(algo_anchor.automatic_mainnet_enabled())
        self.assertEqual(worker.last_confirmed()["size"], 0)
        src = Path(worker.__file__).read_text(encoding="utf-8")
        self.assertNotIn("submit_mainnet_canary", src)
        boot = Path(__file__).resolve().parents[1] / "live402" / "server.py"
        self.assertNotIn("submit_mainnet_canary", boot.read_text(encoding="utf-8"))

    def test_unexpected_non_pq1_is_incident(self):
        from live402.pq import ops_state

        ops_state.reset()
        bad = {
            "pq_auth": b"not-enough",
            "sender": payment.DEFAULT_PAYTO_ALGORAND,
            "receiver": "SOMEOTHERADDRESSAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "amount": 1,
            "fee": 3000,
            "tx_type": "pay",
            "note": b"",
        }
        out = algo_anchor.classify_falcon_account_txn(
            bad, expected_address=payment.DEFAULT_PAYTO_ALGORAND
        )
        self.assertTrue(out["incident"])
        self.assertEqual(out["alert"], "unexpected_non_pq1_txn")
        self.assertEqual(out["severity"], "incident")
        self.assertTrue(ops_state.snapshot()["last_non_pq1_incident"])


class FreshLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.testnet_db = os.path.join(self.tmp.name, "pq-log.sqlite")
        self.mainnet_db = os.path.join(self.tmp.name, "pq-log-mainnet.sqlite")
        clear_pq_env()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        clear_pq_env()
        store.reset()
        self.tmp.cleanup()

    def test_testnet_tree_untouched_production_tree_zero(self):
        os.environ["LIVE402_PQ_LOG_DB"] = self.testnet_db
        os.environ.pop("LIVE402_PQ_LOG_EPOCH", None)
        store.reset()
        for i in range(5):
            store.append(("leaf-%d" % i).encode("ascii"))
        self.assertEqual(store.size(), 5)
        testnet_root = store.root(5)
        store.close()

        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_LOG_DB"] = self.mainnet_db
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN_MAINNET
        store.reset()
        self.assertEqual(store.size(), 0)
        self.assertEqual(store.origin(), ORIGIN_MAINNET)
        rec = store.append(b"first-mainnet")
        self.assertEqual(rec["size"], 1)
        self.assertEqual(rec["idx"], 0)
        self.assertEqual(store.size(), 1)
        store.close()

        os.environ["LIVE402_PQ_LOG_EPOCH"] = "testnet-v1"
        os.environ["LIVE402_PQ_LOG_DB"] = self.testnet_db
        os.environ.pop("LIVE402_PQ_LOG_ORIGIN", None)
        store.close()
        self.assertEqual(store.size(), 5)
        self.assertEqual(store.root(5), testnet_root)
        self.assertEqual(store.origin(), ORIGIN)
        self.assertNotEqual(store.origin(), ORIGIN_MAINNET)
        self.assertTrue(os.path.isfile(self.testnet_db))
        self.assertTrue(os.path.isfile(self.mainnet_db))

    def test_v2_trust_not_live_public(self):
        v1 = trust.public_descriptor()
        self.assertEqual(v1["falcon"]["network"], "testnet-v1.0")
        v2 = trust.trust_root_v2()
        self.assertEqual(v2["epoch"], "mainnet-v1")
        self.assertEqual(v2["origin"], ORIGIN_MAINNET)
        self.assertTrue(v2["not_mainnet_go"])
        self.assertEqual(v2["log_signature"]["reuse_testnet_sk"], False)

    def test_mainnet_epoch_public_descriptor_is_mainnet(self):
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_LOG_DB"] = self.mainnet_db
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN_MAINNET
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = (
            "GVIAG3YMJ7OLJ3JAUBNI2YP5JCQQCQYWN25UAGLC2BTPOBUL3ZZTILIMWU"
        )
        os.environ["LIVE402_PQ_LOG_VKEY"] = "testnet-must-not-win"
        os.environ["LIVE402_PQ_LOG_VKEY_MAINNET"] = ""
        desc = trust.public_descriptor()
        self.assertEqual(desc["falcon"]["network"], "mainnet-v1.0")
        self.assertEqual(desc["falcon"]["allowed_broadcast"], "none")
        self.assertEqual(desc["origin"], ORIGIN_MAINNET)
        self.assertEqual(
            desc["falcon"]["address"],
            "GVIAG3YMJ7OLJ3JAUBNI2YP5JCQQCQYWN25UAGLC2BTPOBUL3ZZTILIMWU",
        )
        self.assertNotEqual(desc["log_signature"].get("vkey"), "testnet-must-not-win")
        self.assertTrue(desc["not_mainnet_go"])

    def test_mainnet_ed25519_vkey_must_not_equal_testnet_public(self):
        from live402.pq import checkpoint as ckpt
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        def _vk(name, key):
            pk = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            return ckpt.vkey_encode(name, pk)

        same = Ed25519PrivateKey.generate()
        other = Ed25519PrivateKey.generate()
        test_v = _vk(ORIGIN, same)
        reused = _vk(ORIGIN_MAINNET, same)
        fresh = _vk(ORIGIN_MAINNET, other)
        with self.assertRaises(log_identity.ConfigError) as ctx:
            log_identity.reject_reused_ed25519_vkey(test_v, reused)
        self.assertIn("reuses testnet", str(ctx.exception))
        log_identity.reject_reused_ed25519_vkey(test_v, fresh)

    def test_env_cleanup_does_not_leak_mainnet_epoch(self):
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        self.assertEqual(log_identity.configured_epoch(), "mainnet-v1")
        clear_pq_env()
        self.assertEqual(log_identity.configured_epoch(), "testnet-v1")
        self.assertEqual(log_identity.configured_network(), "")

    def test_trust_v2_rejects_reuse_testnet_sk(self):
        import copy

        desc = copy.deepcopy(trust.trust_root_v2())
        desc["log_signature"]["reuse_testnet_sk"] = True
        with self.assertRaises(trust.UnknownAlgorithm):
            trust.validate_descriptor_v2(desc)
        desc["log_signature"]["reuse_testnet_sk"] = None
        with self.assertRaises(trust.UnknownAlgorithm):
            trust.validate_descriptor_v2(desc)
        desc["log_signature"].pop("reuse_testnet_sk", None)
        with self.assertRaises(trust.UnknownAlgorithm):
            trust.validate_descriptor_v2(desc)


class RecoveryDrillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clear_pq_env()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        worker.clear_queue()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        worker.clear_queue()
        clear_pq_env()
        store.reset()
        self.tmp.cleanup()

    def test_a_isolated_testnet_backup_restore(self):
        import importlib.util

        store.append(b"archive-leaf")
        src = store.db_path()
        dest_dir = Path(self.tmp.name) / "snap"
        root = Path(__file__).resolve().parents[1]

        def _load(name):
            path = root / "scripts" / ("%s.py" % name)
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        backup = _load("backup_sqlite")
        restore = _load("restore_sqlite")
        os.environ["LIVE402_HISTORY_DB"] = str(Path(self.tmp.name) / "missing-history.sqlite")
        os.environ["LIVE402_CATALOG_DB"] = str(Path(self.tmp.name) / "missing-catalog.sqlite")
        try:
            rc = backup.main(["--dest", str(dest_dir)])
            self.assertIn(rc, (0, 1))
            snaps = list(dest_dir.glob("pq-log-*.sqlite"))
            self.assertEqual(len(snaps), 1)
            restored = Path(self.tmp.name) / "pq-log-restored.sqlite"
            self.assertEqual(restore.main(["--src", str(snaps[0]), "--dest", str(restored), "--force"]), 0)
            import sqlite3

            conn = sqlite3.connect(restored)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM leaves").fetchone()[0], 1)
            conn.close()
            self.assertTrue(os.path.isfile(src))
        finally:
            os.environ.pop("LIVE402_HISTORY_DB", None)
            os.environ.pop("LIVE402_CATALOG_DB", None)

    def test_f_misconfig_never_sends(self):
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ.pop("LIVE402_PQ_FALCON_MAINNET_BROADCAST", None)
        sent = []
        self.assertIsNone(
            algo_anchor.send_if_allowed(
                b"STXN",
                send_fn=lambda b: sent.append(b) or _TXID,
                params={"genesisID": algo_anchor.MAINNET_GENESIS_ID},
            )
        )
        os.environ["LIVE402_PQ_FALCON_MAINNET_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        self.assertIsNone(
            algo_anchor.send_if_allowed(
                b"STXN",
                send_fn=lambda b: sent.append(b) or _TXID,
                params={"genesisID": algo_anchor.MAINNET_GENESIS_ID},
            )
        )
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
