"""One advertised-timeout deadline for verify, probe, and settle."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")
os.environ.pop("LOCAL_FREE", None)

from live402 import deadline, payment, replay
from live402.route import handle_route
from tests.test_pay_replay import _headers_for, _payload


class FakeClock:
    def __init__(self, start=10_000.0):
        self.t = float(start)

    def monotonic(self):
        return self.t

    def advance(self, seconds):
        self.t += float(seconds)


class DeadlineMathTests(unittest.TestCase):
    def test_caps_are_not_legacy_sequential_budgets(self):
        self.assertLess(deadline.VERIFY_CAP_SECONDS, 20.0)
        self.assertLess(deadline.SETTLE_CAP_SECONDS, 45.0)
        sequential = 20.0 + 55.0 + 45.0
        self.assertLess(deadline.PAYMENT_TIMEOUT_SECONDS, sequential)
        self.assertLessEqual(
            deadline.VERIFY_CAP_SECONDS + deadline.SETTLE_RESERVE_SECONDS,
            deadline.PAYMENT_TIMEOUT_SECONDS,
        )

    def test_fake_clock_reserves_settle_time(self):
        clk = FakeClock()
        with patch("live402.clock.monotonic", clk.monotonic):
            accept = {"maxTimeoutSeconds": 60}
            end = deadline.payment_deadline(accept)
            verify_t = deadline.verify_timeout(end)
            self.assertLessEqual(verify_t, deadline.VERIFY_CAP_SECONDS)
            probe_until = deadline.probe_deadline(end)
            self.assertAlmostEqual(end - probe_until, deadline.SETTLE_RESERVE_SECONDS)
            clk.advance(8.0)
            left_for_probe = deadline.remaining(probe_until)
            self.assertGreater(left_for_probe, 0)
            clk.advance(42.0)
            settle_t = deadline.settle_timeout(end)
            self.assertGreater(settle_t, 0)
            self.assertLessEqual(settle_t, deadline.SETTLE_CAP_SECONDS)

    def test_verify_does_not_consume_settle_reserve(self):
        clk = FakeClock()
        with patch("live402.clock.monotonic", clk.monotonic):
            end = deadline.payment_deadline({"maxTimeoutSeconds": 60})
            verify_t = deadline.verify_timeout(end)
            clk.advance(verify_t)
            self.assertGreaterEqual(deadline.remaining(end), deadline.SETTLE_RESERVE_SECONDS - 0.01)


class PaidDeadlinePropagationTests(unittest.TestCase):
    def setUp(self):
        replay.reset()
        os.environ["CDP_ACCESS_TOKEN"] = "test-fixture-token"
        os.environ.pop("LOCAL_FREE", None)

    def tearDown(self):
        replay.reset()
        os.environ.pop("CDP_ACCESS_TOKEN", None)

    def test_verify_and_settle_receive_remaining_slices(self):
        timeouts = []

        def fake_post(url, body, headers=None, timeout=20.0):
            timeouts.append((str(url), float(timeout)))
            if str(url).rstrip("/").endswith("/verify"):
                return 200, {"isValid": True}
            if str(url).rstrip("/").endswith("/settle"):
                return 200, {"success": True}
            return 404, {"error": "unexpected"}

        clk = FakeClock()
        headers = _headers_for(_payload("ff"))
        body = {"need": "weather", "url": "https://fixture.402signal.local/weather"}
        with patch("live402.clock.monotonic", clk.monotonic), patch(
            "live402.facilitator.post_json", side_effect=fake_post
        ):
            code, _result, _extra = handle_route(
                body, headers, "https://402signal.com/route"
            )
        self.assertIn(code, (200, 503))
        verify_t = next(t for u, t in timeouts if u.endswith("/verify"))
        settle_t = next(t for u, t in timeouts if u.endswith("/settle"))
        self.assertLessEqual(verify_t, deadline.VERIFY_CAP_SECONDS)
        self.assertLessEqual(settle_t, deadline.SETTLE_CAP_SECONDS)
        self.assertNotEqual(verify_t, 20.0)
        self.assertNotEqual(settle_t, 45.0)
        self.assertLess(verify_t + settle_t, 20.0 + 45.0)


if __name__ == "__main__":
    unittest.main()
