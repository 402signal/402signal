"""Algod params amplification + pinned MainNet genesis for merchant challenges."""

from __future__ import annotations

import json
import os
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import algod


def _ok_params(round_n=50_000_001):
    return {
        "flatFee": True,
        "fee": 1000,
        "minFee": 1000,
        "firstRound": round_n,
        "lastRound": round_n + algod.VALID_WINDOW,
        "firstValid": round_n,
        "lastValid": round_n + algod.VALID_WINDOW,
        "genesisHash": algod.GENESIS_HASH,
        "genesisID": algod.GENESIS_ID,
    }


class AlgodGenesisPinTests(unittest.TestCase):
    def setUp(self):
        algod.reset_cache()

    def tearDown(self):
        algod.reset_cache()

    def test_wrong_genesis_does_not_alter_challenge(self):
        wrong = json.dumps(
            {
                "last-round": 99,
                "min-fee": 1000,
                "genesis-id": "testnet-v1.0",
                "genesis-hash": "not-mainnet-hash",
            }
        ).encode("utf-8")

        class Resp:
            status = 200

            def read(self, n):
                return wrong[:n]

            def getcode(self):
                return 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Opener:
            def open(self, req, timeout=None):
                return Resp()

        with patch("live402.algod.fixtures.fixture_mode", return_value=False), patch(
            "urllib.request.build_opener", return_value=Opener()
        ):
            out = algod.suggested_params()
        self.assertEqual(out["genesisID"], algod.GENESIS_ID)
        self.assertEqual(out["genesisHash"], algod.GENESIS_HASH)
        self.assertNotEqual(out["genesisID"], "testnet-v1.0")
        self.assertNotIn("not-mainnet-hash", json.dumps(out))

    def test_fetch_rejects_mismatched_genesis(self):
        class Resp:
            status = 200

            def read(self, n):
                return json.dumps(
                    {
                        "last-round": 12,
                        "min-fee": 1000,
                        "genesis-id": "mainnet-v1.0",
                        "genesis-hash": "wrong",
                    }
                ).encode()[:n]

            def getcode(self):
                return 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Opener:
            def open(self, req, timeout=None):
                return Resp()

        with patch("urllib.request.build_opener", return_value=Opener()):
            self.assertIsNone(algod._fetch())


class AlgodCacheTests(unittest.TestCase):
    def setUp(self):
        algod.reset_cache()

    def tearDown(self):
        algod.reset_cache()

    def test_success_cache_avoids_refetch(self):
        calls = []

        def fake_fetch():
            calls.append(1)
            return _ok_params()

        with patch("live402.algod.fixtures.fixture_mode", return_value=False), patch(
            "live402.algod._fetch", side_effect=fake_fetch
        ):
            a = algod.suggested_params()
            b = algod.suggested_params()
        self.assertEqual(len(calls), 1)
        self.assertEqual(a["genesisID"], algod.GENESIS_ID)
        self.assertEqual(b["firstRound"], a["firstRound"])

    def test_negative_cache_does_not_hammer(self):
        calls = []

        def fail():
            calls.append(1)
            return None

        with patch("live402.algod.fixtures.fixture_mode", return_value=False), patch(
            "live402.algod._fetch", side_effect=fail
        ):
            first = algod.suggested_params()
            second = algod.suggested_params()
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["genesisID"], algod.GENESIS_ID)
        self.assertEqual(second["genesisID"], algod.GENESIS_ID)

    def test_singleflight_one_fetch(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow():
            calls.append(1)
            started.set()
            release.wait(timeout=2)
            return _ok_params(77)

        results = []

        def worker():
            results.append(algod.suggested_params())

        with patch("live402.algod.fixtures.fixture_mode", return_value=False), patch(
            "live402.algod._fetch", side_effect=slow
        ):
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            self.assertTrue(started.wait(timeout=2))
            t2.start()
            release.set()
            t1.join(timeout=3)
            t2.join(timeout=3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["firstRound"], 77)
        self.assertEqual(results[1]["firstRound"], 77)

    def test_stale_while_revalidate_returns_last_known(self):
        calls = []

        def fetch():
            calls.append(1)
            return _ok_params(88 + len(calls))

        clk = {"t": 1000.0}

        def mono():
            return clk["t"]

        with patch("live402.algod.fixtures.fixture_mode", return_value=False), patch(
            "live402.algod._fetch", side_effect=fetch
        ), patch("live402.clock.monotonic", side_effect=mono):
            first = algod.suggested_params()
            clk["t"] += algod.CACHE_TTL + 0.1
            second = algod.suggested_params()
        self.assertEqual(first["genesisID"], algod.GENESIS_ID)
        self.assertEqual(second["firstRound"], first["firstRound"])
        self.assertGreaterEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
