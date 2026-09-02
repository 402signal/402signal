"""Compile/import sanity for the pre-key closeout modules. No live network."""

from __future__ import annotations

import ast
import os
import py_compile
import unittest
from pathlib import Path

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402.pq import algo_anchor, canary, mainnet_params, monitor, network as netcfg, signer_mainnet, store, tiles


_ROOT = Path(__file__).resolve().parents[1]
_PY = (
    _ROOT / "live402/pq/network.py",
    _ROOT / "live402/pq/algo_anchor.py",
    _ROOT / "live402/pq/canary.py",
    _ROOT / "live402/pq/signer_mainnet.py",
    _ROOT / "live402/pq/store.py",
    _ROOT / "live402/pq/tiles.py",
    _ROOT / "live402/pq/http.py",
    _ROOT / "live402/pq/monitor.py",
    _ROOT / "live402/pq/mainnet_params.py",
    _ROOT / "live402/algo_tx.py",
    _ROOT / "scripts/pq_mainnet_canary.py",
    _ROOT / "scripts/pq_derive_vkey.py",
    _ROOT / "scripts/pq_public_identity_check.py",
    _ROOT / "scripts/pq_log_fresh_state.py",
)


class CloseoutImportTests(unittest.TestCase):
    def test_compile_and_import(self):
        for path in _PY:
            py_compile.compile(str(path), doraise=True)
        self.assertTrue(callable(netcfg.provider_org))
        self.assertTrue(callable(netcfg.confirmation_independent))
        self.assertTrue(callable(netcfg.confirm_host_allowlisted))
        self.assertTrue(callable(netcfg.runtime_confirmation_independent))
        self.assertTrue(callable(netcfg.confirmation_status))
        self.assertTrue(callable(netcfg.computed_confirmation_policy))
        self.assertTrue(callable(algo_anchor.required_fee))
        self.assertTrue(callable(algo_anchor.canonical_validity))
        self.assertTrue(callable(canary.send_durable))
        self.assertTrue(callable(canary.inspect))
        self.assertTrue(callable(canary.prepare))
        self.assertTrue(callable(canary.send_persisted))
        self.assertTrue(callable(mainnet_params.fetch_trusted_mainnet_params))
        self.assertEqual(signer_mainnet.SIGNER_PROTOCOL, "pq-anchor/2")
        self.assertTrue(callable(signer_mainnet.request_signed))
        self.assertTrue(callable(store.get_tile))
        self.assertEqual(tiles.MAX_TILE_INDEX, 2**63 - 1)
        status = netcfg.confirmation_status("mainnet")
        self.assertFalse(status["confirmation_ready"])
        self.assertFalse(algo_anchor.automatic_mainnet_enabled())
        snap = monitor.snapshot()
        self.assertFalse(snap["confirm_provider"]["confirmation_ready"])
        self.assertTrue(snap["trust"]["not_mainnet_go"])

    def test_network_singular_defs(self):
        tree = ast.parse((_ROOT / "live402/pq/network.py").read_text(encoding="utf-8"))
        names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        for target in (
            "provider_org",
            "confirmation_independent",
            "confirm_host_allowlisted",
            "runtime_confirmation_independent",
            "confirmation_status",
            "configured_confirm_provider",
            "computed_confirmation_policy",
        ):
            self.assertEqual(names.count(target), 1, target)


if __name__ == "__main__":
    unittest.main()
