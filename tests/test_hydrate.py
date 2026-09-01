"""PR15 finalist hydration: slim rows, bounded schemas, claimed ≠ observed."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import catalog, hydrate, payment, probe, select, shadow


def _huge_schema(n: int = 400, desc_len: int = 80) -> dict:
    return {
        "type": "object",
        "properties": {
            ("k%d" % i): {"type": "string", "description": "x" * desc_len} for i in range(n)
        },
    }


def _raw(url: str, schema=None, **extra):
    row = {
        "url": url,
        "description": extra.pop("description", "weather forecast"),
        "serviceName": "Weather API",
        "accepts": extra.pop(
            "accepts",
            [
                {
                    "network": payment.BASE_CAIP2,
                    "payTo": "0xabcabcabcabcabcabcabcabcabcabcabcabcabca",
                    "amount": "20000",
                    "asset": payment.USDC_BASE,
                }
            ],
        ),
        "extensions": {
            "bazaar": {
                "info": {
                    "input": {
                        "method": "POST",
                        "toolName": extra.pop("toolName", "get_weather"),
                        "type": "http",
                        "bodyType": "json",
                    }
                }
            }
        },
    }
    if schema is not None:
        row["inputSchema"] = schema
        row["outputSchema"] = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    row.update(extra)
    return row


class SlimStaysSlimTests(unittest.TestCase):
    def test_slim_item_drops_schema_even_when_stashed(self):
        stash = {}
        schema = {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        }
        slim = catalog.slim_item(_raw("https://wx.example/slim", schema), "base", stash=stash)
        self.assertNotIn("inputSchema", slim)
        self.assertNotIn("outputSchema", slim)
        blob = json.dumps(slim)
        self.assertLess(len(blob), 8000)
        self.assertTrue(slim["_input_schema_present"])
        self.assertIn("https://wx.example/slim", stash)
        contract = stash["https://wx.example/slim"]
        self.assertEqual(contract["origin"], hydrate.ORIGIN_CLAIMED)
        self.assertEqual(contract["tool_name"], "get_weather")
        self.assertEqual(contract["content_type"], "application/json")
        self.assertEqual((contract.get("input_schema") or {}).get("required"), ["city"])


class HydrationBoundTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("LIVE402_CATALOG_DB")
        fd, self._path = tempfile.mkstemp(prefix="live402-hydrate-", suffix=".sqlite")
        os.close(fd)
        os.environ["LIVE402_CATALOG_DB"] = self._path
        shadow.reset()
        hydrate.cache_clear()

    def tearDown(self):
        hydrate.cache_clear()
        shadow.reset()
        try:
            os.remove(self._path)
        except OSError:
            pass
        if self._prev is None:
            os.environ.pop("LIVE402_CATALOG_DB", None)
        else:
            os.environ["LIVE402_CATALOG_DB"] = self._prev

    def test_hydrate_only_top_finalists(self):
        stash = {}
        ranked = []
        for i in range(16):
            raw = _raw(
                "https://wx.example/n%d" % i,
                {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
                toolName="wx_%d" % i,
            )
            slim = catalog.slim_item(raw, "base", stash=stash)
            ranked.append(slim)
        hydrate.hydrate_finalists(ranked, stash=stash, n=8)
        hydrated = [r for r in ranked if r.get("inputSchema")]
        self.assertEqual(len(hydrated), 8)
        for row in ranked[:8]:
            self.assertIn("inputSchema", row)
            self.assertEqual(row["_claimed_contract"]["origin"], hydrate.ORIGIN_CLAIMED)
            self.assertEqual(row["inputSchema"]["required"], ["q"])
        for row in ranked[8:]:
            self.assertNotIn("inputSchema", row)
            self.assertNotIn("_claimed_contract", row)
            self.assertTrue(row.get("_input_schema_present"))

    def test_oversize_schema_is_dropped_not_stored(self):
        huge = _huge_schema(500, 120)
        raw = _json = json.dumps(huge)
        self.assertGreater(len(_json.encode("utf-8")), hydrate.SCHEMA_MAX_BYTES)
        stash = {}
        slim = catalog.slim_item(_raw("https://wx.example/huge", huge), "base", stash=stash)
        ranked = [slim]
        hydrate.hydrate_finalists(ranked, stash=stash, n=5)
        contract = ranked[0].get("_claimed_contract") or {}
        self.assertTrue(contract.get("truncated") or not ranked[0].get("inputSchema"))
        cached = hydrate.cache_get("https://wx.example/huge")
        if cached:
            self.assertIsNone(cached.get("input_schema"))
            self.assertTrue(cached.get("truncated"))

    def test_cache_ttl_and_row_cap(self):
        for i in range(hydrate.CACHE_MAX_ROWS + 12):
            contract = {
                "origin": hydrate.ORIGIN_CLAIMED,
                "method": "POST",
                "content_type": "application/json",
                "tool_name": "t%d" % i,
                "type": "http",
                "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
                "output_schema": None,
                "schema_bytes": 40,
                "truncated": False,
            }
            hydrate.cache_put("https://wx.example/c%d" % i, contract, ttl_s=3600)
        self.assertLessEqual(hydrate.cache_count(), hydrate.CACHE_MAX_ROWS)
        hydrate.cache_put(
            "https://wx.example/expire",
            {
                "origin": hydrate.ORIGIN_CLAIMED,
                "method": "POST",
                "input_schema": {"type": "object"},
                "schema_bytes": 20,
                "truncated": False,
            },
            ttl_s=1,
        )
        self.assertIsNotNone(hydrate.cache_get("https://wx.example/expire"))
        time.sleep(1.1)
        self.assertIsNone(hydrate.cache_get("https://wx.example/expire"))

    def test_claimed_schema_is_not_observed_payment(self):
        catalog_item = catalog.slim_item(
            _raw(
                "https://wx.example/claimed-pay",
                {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
                accepts=[
                    {
                        "network": payment.BASE_CAIP2,
                        "asset": payment.USDC_BASE,
                        "amount": "20000",
                        "payTo": "0xabcabcabcabcabcabcabcabcabcabcabcabcabca",
                    },
                    {
                        "network": payment.SOLANA_MAINNET,
                        "asset": payment.USDC_SOLANA_MINT,
                        "amount": "1000",
                        "payTo": payment.DEFAULT_PAYTO_SOLANA,
                    },
                ],
            ),
            "base",
            stash={},
        )
        hydrate.hydrate_finalists(
            [catalog_item],
            stash={
                "https://wx.example/claimed-pay": hydrate.extract_claimed_contract(
                    _raw(
                        "https://wx.example/claimed-pay",
                        {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
                    )
                )
            },
        )
        envelope = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": payment.BASE_CAIP2,
                    "asset": payment.USDC_BASE,
                    "amount": "20000",
                    "payTo": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "maxTimeoutSeconds": 60,
                }
            ],
        }
        result = {
            "url": "https://wx.example/claimed-pay",
            "live": True,
            "status": 402,
            "has_402_challenge": True,
            "payTo": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "envelope": envelope,
            "accepts": envelope["accepts"],
        }
        result = probe.attach_catalog_fields(result, catalog_item)
        result = probe.attach_invocable_target(result, catalog_item, envelope)
        observed = payment.payment_options_from_result(result)
        self.assertEqual([o.get("rail") for o in observed], ["base"])
        self.assertNotIn("solana", {o.get("rail") for o in observed})
        selected = select.pick_selected_payment(result, "cheapest", None)
        self.assertEqual(selected["rail"], "base")
        self.assertEqual(selected["payTo"], "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        claimed = result.get("claimed") or {}
        self.assertIn("solana", {o.get("rail") for o in (claimed.get("payment_options") or [])})
        self.assertEqual((claimed.get("contract") or {}).get("origin"), hydrate.ORIGIN_CLAIMED)
        target_accepts = (result.get("target") or {}).get("accepts") or []
        self.assertEqual(len(target_accepts), 1)
        self.assertNotEqual(claimed.get("payTo"), selected["payTo"])


if __name__ == "__main__":
    unittest.main()
