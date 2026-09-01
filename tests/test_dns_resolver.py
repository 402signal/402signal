"""Bounded DNS resolver concurrency. Fail-closed SSRF preserved."""

from __future__ import annotations

import os
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import probe


class DnsBoundTests(unittest.TestCase):
    def setUp(self):
        probe.reset_dns_pool()

    def tearDown(self):
        probe.reset_dns_pool()

    def test_timeout_fail_closed(self):
        def hang(*_a, **_k):
            time.sleep(5)
            return []

        with patch("live402.probe.DNS_TIMEOUT", 0.2), patch(
            "socket.getaddrinfo", side_effect=hang
        ):
            t0 = time.monotonic()
            self.assertFalse(probe._resolve_public("this-must-not-hang.invalid"))
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.5)

    def test_many_timeouts_do_not_spawn_unbounded_threads(self):
        def hang(*_a, **_k):
            time.sleep(1.2)
            return []

        before = {
            t.name for t in threading.enumerate() if str(t.name).startswith("dns")
        }
        with patch("live402.probe.DNS_TIMEOUT", 0.15), patch(
            "socket.getaddrinfo", side_effect=hang
        ):
            workers = []

            def lookup():
                try:
                    probe._getaddrinfo_timed("flood.invalid", timeout=0.15)
                except TimeoutError:
                    pass

            for _ in range(24):
                t = threading.Thread(target=lookup)
                workers.append(t)
                t.start()
            time.sleep(0.25)
            dns_now = [
                t
                for t in threading.enumerate()
                if str(t.name).startswith("dns") and t.name not in before
            ]
            self.assertLessEqual(len(dns_now), probe.dns_worker_cap())
            for t in workers:
                t.join(timeout=3)

    def test_ssrf_still_fail_closed(self):
        self.assertIsNone(probe.safe_target("https://127.0.0.1"))
        self.assertIsNone(probe.safe_target("https://10.0.0.1/x"))
        self.assertIsNone(probe.safe_target("http://example.com"))
        self.assertIsNone(probe.safe_target("https://user:pass@example.com/"))


class HostSlotBoundTests(unittest.TestCase):
    def test_host_slot_cache_bounded(self):
        original = dict(probe._host_slots)
        try:
            with probe._host_slots_lock:
                probe._host_slots.clear()
            for i in range(probe.MAX_HOST_SLOT_KEYS + 40):
                probe._host_semaphore("host-%s.example" % i)
            self.assertLessEqual(probe.host_slot_cache_size(), probe.MAX_HOST_SLOT_KEYS)
        finally:
            with probe._host_slots_lock:
                probe._host_slots.clear()
                probe._host_slots.update(original)


if __name__ == "__main__":
    unittest.main()
