"""PR17 deterministic shadow refresh queue. Fake clock. No ML. No 44k walk."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import catalog, payment, shadow


USDC = payment.USDC_BASE
BASE_ACC = {"network": "eip155:8453", "payTo": "0xabcabcabcabcabcabcabcabcabcabcabcabcabca", "amount": "10000", "asset": USDC}


def _item(url, description="weather", **extra):
    accepts = extra.pop("accepts", [dict(BASE_ACC)])
    row = {
        "url": url,
        "description": description,
        "serviceName": extra.pop("serviceName", "Wx"),
        "accepts": accepts,
        "_input_schema_present": extra.pop("_input_schema_present", True),
        "_output_schema_present": extra.pop("_output_schema_present", False),
        "capability": extra.pop("capability", "travel.weather"),
        "tags": extra.pop("tags", ["weather"]),
        "_rail": extra.pop("_rail", "base"),
    }
    row.update({k: v for k, v in extra.items() if v is not None})
    return row


class RefreshQueueTests(unittest.TestCase):
    def setUp(self):
        fd, self._path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self._prev = os.environ.get("LIVE402_CATALOG_DB")
        self._prev_hot = os.environ.get("LIVE402_HOT_REFRESH_S")
        os.environ["LIVE402_CATALOG_DB"] = self._path
        os.environ["LIVE402_HOT_REFRESH_S"] = "300"
        shadow.reset()
        catalog.reset_working_set_peak()

    def tearDown(self):
        shadow.reset()
        if self._prev is None:
            os.environ.pop("LIVE402_CATALOG_DB", None)
        else:
            os.environ["LIVE402_CATALOG_DB"] = self._prev
        if self._prev_hot is None:
            os.environ.pop("LIVE402_HOT_REFRESH_S", None)
        else:
            os.environ["LIVE402_HOT_REFRESH_S"] = self._prev_hot
        for p in (self._path, self._path + "-wal", self._path + "-shm"):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_priority_order_is_documented(self):
        self.assertEqual(
            shadow.refresh_priority_order(),
            (
                "recent_search",
                "recent_route",
                "source_disagreement",
                "price_change",
                "payto_change",
                "schema_change",
                "failed_probe",
                "stale_observation",
                "high_demand_capability",
            ),
        )

    def test_queue_follows_priority_with_fake_clock(self):
        t0 = 1_700_000_000
        now = t0 + 1_000
        search = "https://q.example/search"
        route = "https://q.example/route"
        disagree = "https://q.example/disagree"
        price = "https://q.example/price"
        payto = "https://q.example/payto"
        schema = "https://q.example/schema"
        failed = "https://q.example/failed"
        stale = "https://q.example/stale"
        demand_a = "https://q.example/demand-a"
        demand_b = "https://q.example/demand-b"
        demand = "https://q.example/demand"

        for url in (search, route, disagree, price, payto, schema, failed, stale, demand_a, demand_b, demand):
            cap = "compute.inference" if url.startswith("https://q.example/demand") else "travel.weather"
            shadow.upsert_item(catalog.slim_item(_item(url, capability=cap), "base"), source="cdp", ts=t0)

        shadow.touch_searched([search], ts=now - 10)
        shadow.touch_routed([route], ts=now - 10)

        other = catalog.slim_item(
            _item(
                disagree,
                accepts=[{**BASE_ACC, "amount": "20000", "payTo": "0xdddddddddddddddddddddddddddddddddddddddd"}],
            ),
            "base",
        )
        shadow.upsert_item(other, source="payai", ts=t0)

        shadow.upsert_item(
            catalog.slim_item(_item(price, accepts=[{**BASE_ACC, "amount": "25000"}]), "base"),
            source="cdp",
            ts=t0 + 50,
        )
        shadow.upsert_item(
            catalog.slim_item(
                _item(payto, accepts=[{**BASE_ACC, "payTo": "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"}]),
                "base",
            ),
            source="cdp",
            ts=t0 + 50,
        )
        shadow.upsert_item(
            _item(
                schema,
                _input_schema_present=True,
            ),
            source="cdp",
            ts=t0,
        )
        shadow.upsert_item(
            _item(schema, _input_schema_present=False),
            source="cdp",
            ts=t0 + 50,
        )
        shadow.mark_verified(failed, ts=t0 + 20, ok=False)
        shadow.touch_searched([demand_a, demand_b], ts=now - 20)
        # Fresh successful probe so this URL is not stale_observation / failed_probe.
        shadow.mark_verified(demand, ts=now - 5, ok=True)

        rows = shadow.due_valued(20, ts=now)
        by_url = {r["url"]: r["reason"] for r in rows}
        self.assertEqual(by_url[search], "recent_search")
        self.assertEqual(by_url[route], "recent_route")
        self.assertEqual(by_url[disagree], "source_disagreement")
        self.assertEqual(by_url[price], "price_change")
        self.assertEqual(by_url[payto], "payto_change")
        self.assertEqual(by_url[schema], "schema_change")
        self.assertEqual(by_url[failed], "failed_probe")
        self.assertEqual(by_url[stale], "stale_observation")
        self.assertEqual(by_url[demand], "high_demand_capability")

        ranked = [r["url"] for r in rows if r["url"] in {search, route, disagree, price, payto, schema, failed, stale, demand}]
        self.assertEqual(
            ranked,
            [search, route, disagree, price, payto, schema, failed, stale, demand],
        )

    def test_first_reason_wins_and_limit_bounds(self):
        t0 = 1_700_100_000
        now = t0 + 1_000
        url = "https://q.example/multi"
        shadow.upsert_item(catalog.slim_item(_item(url), "base"), source="cdp", ts=t0)
        shadow.touch_searched([url], ts=now - 5)
        shadow.touch_routed([url], ts=now - 5)
        shadow.mark_verified(url, ts=t0, ok=False)
        rows = shadow.due_valued(1, ts=now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], url)
        self.assertEqual(rows[0]["reason"], "recent_search")
        self.assertLessEqual(len(shadow.due_valued(3, ts=now)), 3)

    def test_missing_probe_outcome_is_not_failed(self):
        t0 = 1_700_200_000
        now = t0 + 1_000
        url = "https://q.example/unknown-probe"
        shadow.upsert_item(catalog.slim_item(_item(url, capability="other.cap"), "base"), source="cdp", ts=t0)
        rows = shadow.due_valued(5, ts=now)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["url"], url)
        self.assertEqual(rows[0]["reason"], "stale_observation")
        self.assertNotEqual(rows[0]["reason"], "failed_probe")

    def test_fresh_claim_is_not_queued(self):
        now = 1_700_300_000
        url = "https://q.example/fresh"
        shadow.upsert_item(catalog.slim_item(_item(url), "base"), source="cdp", ts=now)
        shadow.touch_searched([url], ts=now)
        self.assertEqual(shadow.due_valued(5, ts=now), [])

    def test_after_refresh_clock_advances_url_drops(self):
        t0 = 1_700_400_000
        now = t0 + 1_000
        url = "https://q.example/refreshed"
        shadow.upsert_item(catalog.slim_item(_item(url), "base"), source="cdp", ts=t0)
        shadow.touch_searched([url], ts=now - 10)
        self.assertEqual([r["url"] for r in shadow.due_valued(5, ts=now)], [url])
        shadow.upsert_item(catalog.slim_item(_item(url), "base"), source="cdp", ts=now)
        self.assertEqual(shadow.due_valued(5, ts=now), [])

    def test_trickle_uses_valued_queue_not_world_walk(self):
        t0 = 1_700_500_000
        now = t0 + 1_000
        url = "https://q.example/trickle"
        shadow.upsert_item(catalog.slim_item(_item(url), "base"), source="cdp", ts=t0)
        shadow.touch_searched([url], ts=now - 10)
        catalog.reset_working_set_peak()
        refreshed = []

        def fake_refresh(dest):
            refreshed.append(dest)

        with patch("live402.catalog.fixtures.fixture_mode", return_value=False), patch(
            "live402.catalog._refresh_disabled", return_value=False
        ), patch("live402.catalog._refresh_url_claims", side_effect=fake_refresh), patch(
            "live402.shadow._now", return_value=now
        ):
            kind = catalog.trickle_once()
        self.assertEqual(kind, "recent_search")
        self.assertEqual(refreshed, [url])
        self.assertLess(catalog.working_set_peak(), 44_000)
        self.assertLessEqual(shadow.QUEUE_SCAN, 20)

    def test_due_hot_helper_still_works(self):
        t0 = 1_700_600_000
        now = t0 + 1_000
        url = "https://q.example/hot-helper"
        shadow.upsert_item(catalog.slim_item(_item(url), "base"), source="cdp", ts=t0)
        shadow.touch_searched([url], ts=now - 10)
        self.assertEqual(shadow.due_hot(5, ts=now), [url])


if __name__ == "__main__":
    unittest.main()
