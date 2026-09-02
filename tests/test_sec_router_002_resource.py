"""SEC-ROUTER-002: match_accept binds resource.url and fails closed on mismatch.

A payment authorized for /route must not match /mcp. No live spend.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")
os.environ.pop("LOCAL_FREE", None)

from live402 import discover, payment, replay
from live402.route import handle_route
from tests.test_pay_replay import (
    _counting_facilitator,
    _headers_for,
    _payload,
    _weather_body,
)

ROUTE = discover.ROUTE
MCP = discover.ORIGIN + "/mcp"


def _required(resource_url):
    return payment.payment_required(resource_url)


class ResourceUrlOfTests(unittest.TestCase):
    def test_v2_object_and_v1_string(self):
        self.assertEqual(payment.resource_url_of({"resource": {"url": ROUTE}}), ROUTE)
        self.assertEqual(payment.resource_url_of({"resource": MCP}), MCP)
        self.assertIsNone(payment.resource_url_of({"resource": {"url": "  "}}))
        self.assertIsNone(payment.resource_url_of({}))
        self.assertIsNone(payment.resource_url_of(None))


class MatchAcceptResourceBindTests(unittest.TestCase):
    def test_same_resource_matches(self):
        accept = payment.match_accept(_payload("ok", resource_url=ROUTE), _required(ROUTE))
        self.assertIsInstance(accept, dict)
        self.assertEqual(payment.rail_of_accept(accept), "base")

    def test_route_payment_does_not_match_mcp(self):
        accept = payment.match_accept(_payload("xr", resource_url=ROUTE), _required(MCP))
        self.assertIsNone(accept)

    def test_mcp_payment_does_not_match_route(self):
        accept = payment.match_accept(_payload("mx", resource_url=MCP), _required(ROUTE))
        self.assertIsNone(accept)

    def test_missing_payload_resource_fails_closed(self):
        accept = payment.match_accept(_payload("nr", resource_url=""), _required(ROUTE))
        self.assertIsNone(accept)

    def test_missing_required_resource_fails_closed(self):
        required = _required(ROUTE)
        required = dict(required)
        required.pop("resource", None)
        accept = payment.match_accept(_payload("mr", resource_url=ROUTE), required)
        self.assertIsNone(accept)

    def test_accepted_resource_mismatch_fails_closed(self):
        body = _payload("ar", resource_url=ROUTE)
        body["accepted"] = dict(body["accepted"])
        body["accepted"]["resource"] = MCP
        self.assertIsNone(payment.match_accept(body, _required(ROUTE)))

    def test_v1_string_resource_binds(self):
        body = _payload("v1", resource_url="")
        body["resource"] = ROUTE
        accept = payment.match_accept(body, _required(ROUTE))
        self.assertIsInstance(accept, dict)
        self.assertIsNone(payment.match_accept(body, _required(MCP)))


class RouteMcpReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._prev_db = os.environ.get("LIVE402_REPLAY_DB")
        os.environ["LIVE402_REPLAY_DB"] = os.path.join(self.tmp.name, "replay.sqlite")
        replay.reset()
        os.environ["CDP_ACCESS_TOKEN"] = "test-fixture-token"
        os.environ.pop("LOCAL_FREE", None)

    def tearDown(self):
        replay.reset()
        os.environ.pop("CDP_ACCESS_TOKEN", None)
        if self._prev_db is None:
            os.environ.pop("LIVE402_REPLAY_DB", None)
        else:
            os.environ["LIVE402_REPLAY_DB"] = self._prev_db
        self.tmp.cleanup()

    def test_route_auth_rejected_on_mcp_no_second_verify(self):
        """PAYMENT-SIGNATURE for /route against /mcp: 402, no verify, no settle."""
        verify_calls = []
        settle_calls = []
        headers = _headers_for(_payload("rb", resource_url=ROUTE))
        body = _weather_body()
        with patch(
            "live402.facilitator.post_json",
            side_effect=_counting_facilitator(verify_calls, settle_calls),
        ):
            first = handle_route(body, headers, ROUTE)
            second = handle_route(body, headers, MCP, bazaar=payment.BAZAAR_MCP)
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 402)
        self.assertEqual(len(settle_calls), 1)
        self.assertEqual(len(verify_calls), 1)
        self.assertEqual(
            (second[1] or {}).get("error"),
            "Payment does not match an advertised accept",
        )


if __name__ == "__main__":
    unittest.main()
