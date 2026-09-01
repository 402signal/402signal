"""Static confirm providers + split independence vs readiness. No live network."""

from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402.pq import algo_anchor, monitor, trust
from live402.pq import network as netcfg
from tests.pq_test_env import clear_pq_env


class ConfirmOrgMappingTests(unittest.TestCase):
    def setUp(self):
        clear_pq_env()
        self.addCleanup(clear_pq_env)

    def test_same_org_false(self):
        self.assertFalse(
            netcfg.confirmation_independent(
                "mainnet-api.algonode.cloud", "mainnet-idx.algonode.cloud"
            )
        )
        self.assertFalse(
            netcfg.confirmation_independent(
                "mainnet-api.algonode.cloud", "mainnet-idx.4160.nodely.dev"
            )
        )
        self.assertEqual(netcfg.provider_org("mainnet-idx.4160.nodely.dev"), "nodely")
        self.assertEqual(netcfg.provider_org("mainnet-api.algonode.cloud"), "nodely")

    def test_unknown_org_false(self):
        self.assertEqual(netcfg.provider_org("unknown.example"), "")
        self.assertFalse(
            netcfg.confirmation_independent("mainnet-api.algonode.cloud", "unknown.example")
        )
        self.assertFalse(netcfg.confirmation_independent("mystery.a", "mystery.b"))

    def test_different_known_org_true(self):
        orgs = {
            "mainnet-api.algonode.cloud": netcfg.ORG_NODELY,
            "algod.example-signal": netcfg.ORG_SIGNAL,
        }
        self.assertTrue(
            netcfg.confirmation_independent(
                "mainnet-api.algonode.cloud", "algod.example-signal", orgs=orgs
            )
        )
        self.assertFalse(
            netcfg.confirmation_independent(
                "mainnet-api.algonode.cloud", "mainnet-idx.4160.nodely.io", orgs=orgs
            )
        )

    def test_default_trust_v2_stays_pre_go(self):
        v2 = trust.trust_root_v2()
        self.assertFalse(v2["confirmation_policy"]["independent_provider"])
        self.assertTrue(v2["not_mainnet_go"])
        confirm = algo_anchor.confirm_provider("mainnet")
        self.assertFalse(confirm["independent_of_submit"])
        self.assertEqual(confirm["org"], "nodely")
        self.assertFalse(netcfg.CONFIRM_INDEPENDENT_OF_SUBMIT)
        self.assertFalse(netcfg.runtime_confirmation_independent("mainnet"))
        self.assertFalse(confirm["confirmation_ready"])
        self.assertFalse(confirm["confirm_provider_known"])
        self.assertFalse(confirm["confirm_falcon_compatible"])

    def test_unknown_provider_enum_fails_closed(self):
        os.environ["LIVE402_PQ_CONFIRM_PROVIDER"] = "blockdaemon"
        with self.assertRaises(netcfg.UnknownNetwork):
            netcfg.configured_confirm_provider()
        status = netcfg.confirmation_status("mainnet")
        self.assertFalse(status["confirm_provider_known"])
        self.assertFalse(status["confirm_org_independent"])
        self.assertFalse(status["confirmation_ready"])
        os.environ["LIVE402_PQ_CONFIRM_PROVIDER"] = "not-a-provider"
        with self.assertRaises(netcfg.UnknownNetwork):
            netcfg.configured_confirm_host("mainnet")

    def test_generic_url_env_is_ignored(self):
        os.environ["LIVE402_PQ_CONFIRM_TXN_URL"] = "https://evil.example/v2/transactions/X"
        os.environ["LIVE402_PQ_CONFIRM_HOST"] = "evil.example"
        host = netcfg.configured_confirm_host("mainnet")
        url = netcfg.configured_confirm_txn_url("mainnet", "A" * 52)
        self.assertEqual(host, netcfg.MAINNET.confirm_host)
        self.assertTrue(url.startswith(netcfg.MAINNET.confirm_txn_url))
        self.assertNotIn("evil.example", url)
        src = Path(netcfg.__file__).read_text(encoding="utf-8")
        self.assertNotIn("CONFIRM_URL_ENV", src)
        self.assertNotIn("CONFIRM_HOST_ENV", src)

    def test_tatum_table_is_static(self):
        os.environ["LIVE402_PQ_CONFIRM_PROVIDER"] = "tatum"
        provider = netcfg.configured_confirm_provider()
        self.assertEqual(provider.host, "algorand-mainnet-indexer.gateway.tatum.io")
        self.assertEqual(provider.org, "tatum")
        self.assertEqual(provider.path_template, "/v2/transactions/{txid}")
        self.assertEqual(provider.auth_header, "x-api-key")
        self.assertEqual(provider.secret_env, "LIVE402_PQ_CONFIRM_TATUM_API_KEY")
        url = netcfg.configured_confirm_txn_url("mainnet", "B" * 52)
        self.assertEqual(
            url,
            "https://algorand-mainnet-indexer.gateway.tatum.io/v2/transactions/" + ("B" * 52),
        )
        self.assertTrue(url.startswith("https://"))
        self.assertNotIn("api-key", url.lower())
        self.assertFalse(netcfg.credentials_configured())
        self.assertIsNone(netcfg.confirm_auth_header())

    def test_nownodes_table_is_static(self):
        os.environ["LIVE402_PQ_CONFIRM_PROVIDER"] = "nownodes"
        provider = netcfg.configured_confirm_provider()
        self.assertEqual(provider.host, "algo-index.nownodes.io")
        self.assertEqual(provider.org, "nownodes")
        self.assertEqual(provider.auth_header, "api-key")
        self.assertEqual(provider.secret_env, "LIVE402_PQ_CONFIRM_NOWNODES_API_KEY")
        url = netcfg.configured_confirm_txn_url("mainnet", "C" * 52)
        self.assertEqual(url, "https://algo-index.nownodes.io/v2/transactions/" + ("C" * 52))

    def test_tatum_runtime_independent_does_not_rewrite_trust_or_ready(self):
        os.environ["LIVE402_PQ_CONFIRM_PROVIDER"] = "tatum"
        self.assertTrue(netcfg.runtime_confirmation_independent("mainnet"))
        status = netcfg.confirmation_status("mainnet")
        self.assertTrue(status["confirm_provider_known"])
        self.assertTrue(status["confirm_org_independent"])
        self.assertFalse(status["confirm_credentials_configured"])
        self.assertFalse(status["confirm_reachable"])
        self.assertFalse(status["confirm_falcon_compatible"])
        self.assertFalse(status["confirmation_ready"])
        self.assertEqual(status["blocker"], "tatum_falcon_pqsig_unproven_no_api_key")
        confirm = algo_anchor.confirm_provider("mainnet")
        self.assertTrue(confirm["independent_of_submit"])
        self.assertFalse(confirm["confirmation_ready"])
        v2 = trust.trust_root_v2()
        self.assertFalse(v2["confirmation_policy"]["independent_provider"])
        self.assertTrue(v2["not_mainnet_go"])
        trust.validate_descriptor_v2(v2)
        snap = monitor.snapshot()
        self.assertFalse(snap["confirm_provider"]["confirmation_ready"])
        self.assertTrue(snap["trust"]["not_mainnet_go"])

    def test_credentials_without_falcon_proof_still_not_ready(self):
        os.environ["LIVE402_PQ_CONFIRM_PROVIDER"] = "tatum"
        os.environ["LIVE402_PQ_CONFIRM_TATUM_API_KEY"] = "named-not-logged"
        status = netcfg.confirmation_status("mainnet")
        self.assertTrue(status["confirm_credentials_configured"])
        self.assertFalse(status["confirm_falcon_compatible"])
        self.assertFalse(status["confirmation_ready"])
        header = netcfg.confirm_auth_header()
        self.assertEqual(header[0], "x-api-key")
        blob = str(status) + str(monitor.snapshot())
        self.assertNotIn("named-not-logged", blob)

    def test_no_blockdaemon_and_singular_defs(self):
        src = Path(netcfg.__file__).read_text(encoding="utf-8")
        self.assertNotIn("blockdaemon", netcfg.CONFIRM_PROVIDERS)
        self.assertNotIn("blockdaemon", netcfg.PROVIDER_ORGS.values())
        tree = ast.parse(src)
        names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        for target in (
            "provider_org",
            "confirmation_independent",
            "confirm_host_allowlisted",
            "runtime_confirmation_independent",
            "confirmation_status",
        ):
            self.assertEqual(names.count(target), 1, target)
        self.assertFalse(netcfg.CONFIRM_FALCON_COMPATIBLE["tatum"])
        self.assertFalse(netcfg.CONFIRM_FALCON_COMPATIBLE["nownodes"])


if __name__ == "__main__":
    unittest.main()
