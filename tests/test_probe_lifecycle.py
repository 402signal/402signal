"""Probe lifecycle: workers do not persist; coordinator seals; process-wide cap."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import history, payment, probe


def _usdc(rail="base"):
    return payment.usdc_asset_for_rail(rail) or payment.USDC_BASE


def _item(url, amount="10000", network="base"):
    return {
        "url": url,
        "description": "weather forecast",
        "accepts": [
            {
                "network": network,
                "payTo": "0xabc",
                "amount": amount,
                "asset": _usdc(network),
            }
        ],
    }


def _live(url, amount=10000, latency=10):
    return {
        "live": True,
        "url": url,
        "payTo": "0xabc",
        "amount": amount,
        "asset": _usdc(),
        "latency_ms": latency,
        "invocable": False,
        "payTo_changed": False,
        "probed_at": probe.now_iso(),
        "readiness": "payable",
        "rail": "base",
        "status": 402,
        "has_402_challenge": True,
        "accepts": [
            {
                "network": "eip155:8453",
                "asset": _usdc(),
                "payTo": "0xabc",
                "amount": amount,
            }
        ],
    }


def _dead(url, miss="no_402_envelope"):
    return {
        "live": False,
        "url": url,
        "payTo": None,
        "invocable": False,
        "miss_reason": miss,
        "probed_at": probe.now_iso(),
        "status": None,
        "has_402_challenge": False,
    }


def _obs_urls(batch_id):
    conn = sqlite3.connect(history.db_path())
    try:
        rows = conn.execute(
            "SELECT DISTINCT url FROM observations WHERE batch_id = ? AND source_type = ?",
            (batch_id, history.SOURCE_OBSERVED),
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _obs_count(batch_id):
    conn = sqlite3.connect(history.db_path())
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE batch_id = ? AND source_type = ?",
            (batch_id, history.SOURCE_OBSERVED),
        ).fetchone()[0]
        return int(n or 0)
    finally:
        conn.close()


def _probe_count(url):
    conn = sqlite3.connect(history.db_path())
    try:
        n = conn.execute("SELECT COUNT(*) FROM probes WHERE url = ?", (url,)).fetchone()[0]
        return int(n or 0)
    finally:
        conn.close()


class ProbeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._prev_db = os.environ.get("LIVE402_HISTORY_DB")
        fd, self._db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.environ["LIVE402_HISTORY_DB"] = self._db
        history.reset()
        probe.reset_probe_inflight_peak()

    def tearDown(self):
        history.reset()
        if self._prev_db is None:
            os.environ.pop("LIVE402_HISTORY_DB", None)
        else:
            os.environ["LIVE402_HISTORY_DB"] = self._prev_db
        for path in (self._db, self._db + "-wal", self._db + "-shm"):
            try:
                os.remove(path)
            except OSError:
                pass

    def _route(self, items, fake_probe, need="weather", **kwargs):
        with patch("live402.probe.fetch_discovery", return_value=items), patch(
            "live402.probe.probe_url", side_effect=fake_probe
        ):
            return probe.route_need(need, **kwargs)

    def test_workers_probe_with_record_false(self):
        items = [
            _item("https://a.lifecycle.example/weather"),
            _item("https://b.lifecycle.example/weather"),
        ]
        seen = []

        def fake_probe(url, catalog_item=None, deadline=None, record=True, batch_id=None, **kwargs):
            seen.append({"url": url, "record": record, "batch_id": batch_id})
            return _live(url)

        result = self._route(items, fake_probe)
        self.assertTrue(result.get("live"))
        self.assertTrue(seen)
        self.assertTrue(all(row["record"] is False for row in seen))
        self.assertTrue(all(row["batch_id"] is None for row in seen))

    def test_coordinator_persists_exactly_completed_observations(self):
        items = [
            _item("https://one.lifecycle.example/weather"),
            _item("https://two.lifecycle.example/weather"),
            _item("https://three.lifecycle.example/weather"),
        ]

        def fake_probe(url, catalog_item=None, deadline=None, record=True, **kwargs):
            return _live(url)

        result = self._route(items, fake_probe)
        bid = result.get("batch_id")
        self.assertTrue(bid)
        self.assertTrue(history.batch_is_sealed(bid))
        self.assertEqual(
            _obs_urls(bid),
            {item["url"] for item in items},
        )
        self.assertEqual(result.get("candidates_probed"), 3)

    def test_history_accumulates_for_accepted_probes(self):
        url = "https://hist.lifecycle.example/weather"
        history.record_probe(
            url,
            {
                "live": True,
                "status": 402,
                "latency_ms": 10,
                "payTo": "0xabc",
                "batch_id": "seedbatch",
            },
        )
        before = history.summary(url)
        self.assertEqual(int(before.get("n_7d") or 0), 1)

        def fake_probe(dest, catalog_item=None, deadline=None, record=True, **kwargs):
            return _live(dest)

        result = self._route([_item(url)], fake_probe)
        self.assertTrue(result.get("live"))
        after = history.summary(url)
        self.assertEqual(int(after.get("n_7d") or 0), 2)
        self.assertEqual(_probe_count(url), 2)

    def test_attestation_hash_stable_after_straggler_record(self):
        items = [
            _item("https://kept.lifecycle.example/weather"),
            _item("https://also.lifecycle.example/weather"),
        ]

        def fake_probe(url, catalog_item=None, deadline=None, record=True, **kwargs):
            return _live(url)

        result = self._route(items, fake_probe)
        bid = result["batch_id"]
        first = history.attestation_for(bid)
        self.assertIsNotNone(first)
        n_before = _obs_count(bid)

        history.record_probe(
            "https://straggler.lifecycle.example/weather",
            {
                "live": True,
                "status": 402,
                "latency_ms": 9,
                "payTo": "0xabc",
                "batch_id": bid,
            },
        )
        again = history.attestation_for(bid)
        self.assertEqual(again["hash"], first["hash"])
        self.assertEqual(again["n"], first["n"])
        self.assertEqual(_obs_count(bid), n_before)
        self.assertNotIn("https://straggler.lifecycle.example/weather", _obs_urls(bid))
        self.assertEqual(_probe_count("https://straggler.lifecycle.example/weather"), 0)

    def test_running_straggler_cannot_append_to_finalized_batch(self):
        items = [
            _item("https://fast-a.lifecycle.example/weather"),
            _item("https://fast-b.lifecycle.example/weather"),
            _item("https://slow.lifecycle.example/weather"),
        ]
        release_slow = threading.Event()
        slow_started = threading.Event()
        slow_done = threading.Event()

        def fake_probe(url, catalog_item=None, deadline=None, record=True, **kwargs):
            if "slow" in url:
                slow_started.set()
                release_slow.wait(2.0)
                slow_done.set()
                return _live(url, latency=80)
            return _live(url, latency=5)

        result = self._route(items, fake_probe, deadline=time.monotonic() + 0.25)
        self.assertTrue(slow_started.wait(1.0))
        bid = result.get("batch_id")
        self.assertTrue(bid)
        self.assertTrue(history.batch_is_sealed(bid))
        self.assertNotIn("https://slow.lifecycle.example/weather", _obs_urls(bid))
        first = history.attestation_for(bid)
        self.assertIsNotNone(first)

        # Simulate the still-running worker finishing and trying to persist
        # into the route batch_id (the pre-fix failure mode).
        history.record_probe(
            "https://slow.lifecycle.example/weather",
            {
                "live": True,
                "status": 402,
                "latency_ms": 80,
                "payTo": "0xabc",
                "batch_id": bid,
            },
        )
        release_slow.set()
        self.assertTrue(slow_done.wait(2.0))
        again = history.attestation_for(bid)
        self.assertEqual(again["hash"], first["hash"])
        self.assertNotIn("https://slow.lifecycle.example/weather", _obs_urls(bid))

    def test_persist_route_batch_is_noop_after_seal(self):
        history.persist_route_batch(
            "sealedone",
            [_live("https://first.lifecycle.example/weather")],
        )
        first = history.attestation_for("sealedone")
        history.persist_route_batch(
            "sealedone",
            [_live("https://second.lifecycle.example/weather")],
        )
        again = history.attestation_for("sealedone")
        self.assertEqual(again["hash"], first["hash"])
        self.assertEqual(_obs_urls("sealedone"), {"https://first.lifecycle.example/weather"})

    def test_process_wide_cap_bounds_concurrent_route_probes(self):
        peak = {"n": 0, "cur": 0}
        lock = threading.Lock()
        batches = [
            [_item("https://cap%d-%d.lifecycle.example/weather" % (idx, j)) for j in range(3)]
            for idx in range(4)
        ]
        disc_state = {"n": 0}

        def fake_disc(need, prefer_network=None):
            _ = need, prefer_network
            with lock:
                idx = disc_state["n"]
                disc_state["n"] += 1
            return batches[idx % 4]

        def fake_probe(url, catalog_item=None, deadline=None, record=True, **kwargs):
            with lock:
                peak["cur"] += 1
                if peak["cur"] > peak["n"]:
                    peak["n"] = peak["cur"]
            time.sleep(0.12)
            with lock:
                peak["cur"] -= 1
            return _live(url)

        probe.reset_probe_inflight_peak()
        with patch("live402.probe.fetch_discovery", side_effect=fake_disc), patch(
            "live402.probe.probe_url", side_effect=fake_probe
        ):
            threads = [
                threading.Thread(target=probe.route_need, args=("weather",))
                for _ in range(4)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5.0)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertLessEqual(peak["n"], probe.MAX_PROCESS_PROBES)
        self.assertGreater(peak["n"], 0)
        self.assertLessEqual(probe.process_probe_inflight_peak(), probe.MAX_PROCESS_PROBES)
        pool = probe._shared_probe_pool()
        self.assertLessEqual(len(getattr(pool, "_threads", [])), probe.MAX_PROCESS_PROBES)
        self.assertEqual(probe.MAX_IN_FLIGHT, 3)
        self.assertLessEqual(probe.MAX_PROCESS_PROBES, 12)
        self.assertGreaterEqual(probe.MAX_PROCESS_PROBES, 8)


if __name__ == "__main__":
    unittest.main()
