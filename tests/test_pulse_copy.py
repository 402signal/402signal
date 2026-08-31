"""PR17 pulse copy: hybrid discovery is true. No 'no local index' lie."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import catalog, pulse, shadow


BANNED_INDEX_LIES = (
    "does not store a local index",
    "do not store a local index",
    "no local index",
    "does not keep a local catalog",
    "we do not keep a local catalog mirror",
)


class PulseCopyHonestyTests(unittest.TestCase):
    def setUp(self):
        fd, self._path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self._prev = os.environ.get("LIVE402_CATALOG_DB")
        os.environ["LIVE402_CATALOG_DB"] = self._path
        shadow.reset()
        pulse.reset_cache()

    def tearDown(self):
        pulse.reset_cache()
        shadow.reset()
        if self._prev is None:
            os.environ.pop("LIVE402_CATALOG_DB", None)
        else:
            os.environ["LIVE402_CATALOG_DB"] = self._prev
        for p in (self._path, self._path + "-wal", self._path + "-shm"):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_insight_no_longer_claims_no_local_index(self):
        text = pulse._upstream_insight("base")
        low = text.lower()
        for lie in BANNED_INDEX_LIES:
            self.assertNotIn(lie, low, lie)
        self.assertIn("upstream", low)
        self.assertIn("shadow", low)
        self.assertNotIn("catalog.sqlite", low)
        self.assertNotIn("/data/", low)
        self.assertNotIn("44k", low)
        self.assertNotIn("14376", text)
        self.assertNotIn("14,000", text)

    def test_empty_shadow_is_upstream_live_not_no_catalog(self):
        self.assertEqual(shadow.resource_count("active"), 0)
        self.assertEqual(pulse.index_status(), pulse.INDEX_UPSTREAM)
        self.assertNotEqual(pulse.index_status(), "upstream")
        with patch("live402.pulse.fixtures.fixture_mode", return_value=False):
            payload = pulse._collect()
        self.assertEqual(payload.get("index_status"), pulse.INDEX_UPSTREAM)
        for chain in pulse.CHAINS:
            insight = (payload["chains"][chain].get("insight") or "").lower()
            for lie in BANNED_INDEX_LIES:
                self.assertNotIn(lie, insight, lie)
            self.assertIn("shadow", insight)
            self.assertIsNone(payload["chains"][chain].get("count"))

    def test_warm_shadow_is_both(self):
        slim = catalog.slim_item(
            {
                "url": "https://wx.example/forecast",
                "description": "hourly weather",
                "serviceName": "Wx",
                "accepts": [{"network": "eip155:8453", "payTo": "0xabc", "amount": "10000"}],
                "capability": "travel.weather",
                "_rail": "base",
            },
            "base",
        )
        shadow.upsert_item(slim, source="cdp")
        self.assertGreater(shadow.resource_count("active"), 0)
        self.assertEqual(pulse.index_status(), pulse.INDEX_BOTH)
        with patch("live402.pulse.fixtures.fixture_mode", return_value=False):
            payload = pulse._collect()
        self.assertEqual(payload.get("index_status"), pulse.INDEX_BOTH)
        self.assertNotEqual(payload.get("index_status"), "ready")
        self.assertNotEqual(payload.get("index_status"), "pending")
        self.assertNotEqual(payload.get("index_status"), "refreshing")
        blob = json_blob(payload)
        self.assertNotIn("catalog.sqlite", blob)
        self.assertNotIn("/data/catalog", blob)
        for chain in pulse.CHAINS:
            self.assertIsNone(payload["chains"][chain].get("count"))
            insight = payload["chains"][chain].get("insight") or ""
            for lie in BANNED_INDEX_LIES:
                self.assertNotIn(lie, insight.lower(), lie)

    def test_fixture_status_stays_fixture(self):
        self.assertTrue(pulse.fixtures.fixture_mode())
        payload = pulse._collect()
        self.assertEqual(payload.get("index_status"), pulse.INDEX_FIXTURE)

    def test_index_status_never_implies_no_local_catalog(self):
        self.assertEqual(pulse.INDEX_STATUSES, (
            "upstream-live",
            "shadow-warm",
            "both",
            "fixture",
        ))
        self.assertNotIn("upstream", pulse.INDEX_STATUSES)
        self.assertNotIn("ready", pulse.INDEX_STATUSES)
        with patch("live402.pulse._shadow_warm", return_value=True), patch(
            "live402.pulse._upstream_configured", return_value=False
        ):
            self.assertEqual(pulse.index_status(), pulse.INDEX_SHADOW)


def json_blob(payload: dict) -> str:
    import json

    return json.dumps(payload)


if __name__ == "__main__":
    unittest.main()
