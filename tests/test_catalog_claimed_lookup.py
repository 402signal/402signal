"""Production claimed URL lookup is exact local catalog, not fixtures."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import catalog, fixtures, route, validate


class ClaimedLookupTests(unittest.TestCase):
    def test_production_skips_fixtures_and_remote_search(self):
        fixture_row = {"url": "https://fixture.example/x", "accepts": []}
        shadow_row = {"url": "https://shadow.example/x", "_rail": "base"}
        with patch("live402.route.fixtures.fixture_mode", return_value=False), patch(
            "live402.route.fixtures.lookup_url", return_value=fixture_row
        ) as lookup, patch(
            "live402.catalog.claimed_item_for_url", return_value=shadow_row
        ) as claimed, patch(
            "live402.catalog.item_for_url", return_value={"url": "https://remote.example/x"}
        ) as remote:
            got = route._lookup_claimed("https://shadow.example/x")
        self.assertIs(got, shadow_row)
        lookup.assert_not_called()
        claimed.assert_called_once_with("https://shadow.example/x")
        remote.assert_not_called()

    def test_validate_production_uses_claimed_only(self):
        shadow_row = {"url": "https://shadow.example/x"}
        with patch("live402.validate.fixtures.fixture_mode", return_value=False), patch(
            "live402.validate.fixtures.lookup_url", return_value={"url": "https://fixture.example/x"}
        ) as lookup, patch(
            "live402.validate.catalog.claimed_item_for_url", return_value=shadow_row
        ), patch(
            "live402.validate.catalog.item_for_url", return_value={"url": "https://remote.example/x"}
        ) as remote:
            got = validate.catalog_item_for("https://shadow.example/x")
        self.assertIs(got, shadow_row)
        lookup.assert_not_called()
        remote.assert_not_called()

    def test_fixture_mode_still_uses_fixtures(self):
        with patch("live402.route.fixtures.fixture_mode", return_value=True), patch(
            "live402.route.fixtures.lookup_url", return_value={"url": "https://fx.example/x"}
        ) as lookup, patch("live402.catalog.claimed_item_for_url") as claimed:
            got = route._lookup_claimed("https://fx.example/x")
        self.assertEqual(got["url"], "https://fx.example/x")
        lookup.assert_called_once()
        claimed.assert_not_called()

    def test_claimed_item_for_url_is_shadow_only(self):
        with patch("live402.catalog.shadow.get_resource", return_value={"url": "https://s.example/x"}) as get:
            got = catalog.claimed_item_for_url("https://s.example/x")
        self.assertEqual(got["url"], "https://s.example/x")
        get.assert_called_once_with("https://s.example/x")

    def test_item_for_url_does_not_use_fixtures_outside_fixture_mode(self):
        with patch("live402.catalog.fixtures.fixture_mode", return_value=False), patch(
            "live402.catalog.fixtures.lookup_url"
        ) as lookup, patch(
            "live402.catalog.shadow.get_resource", return_value={"url": "https://local.example/x"}
        ):
            got = catalog.item_for_url("https://local.example/x")
        self.assertEqual(got["url"], "https://local.example/x")
        lookup.assert_not_called()


class FixturesHelperStillWorks(unittest.TestCase):
    def test_lookup_url_function_exists(self):
        self.assertTrue(callable(fixtures.lookup_url))


if __name__ == "__main__":
    unittest.main()
