"""SEC-ROUTER-004 / A-14: route success vs log append non-atomicity.

Paid 200/503 does not require a durable signed leaf unless
require_transparency. Env vkey wins over stale sqlite meta.vkey.
No live spend.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")
os.environ.pop("LOCAL_FREE", None)

from live402 import discover, replay
from live402.pq import receipt, store
from live402.pq import transparency as pq_view
from live402.route import handle_route
from tests.test_pay_replay import _fake_facilitator, _headers_for, _payload, _weather_body

ROUTE = discover.ROUTE


class PublicVkeyEnvWinsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._prev = {
            key: os.environ.get(key)
            for key in (
                "LIVE402_PQ_LOG_DB",
                "LIVE402_PQ_LOG_VKEY",
                "LIVE402_PQ_LOG_VKEY_MAINNET",
                "LIVE402_PQ_LOG_EPOCH",
                "LIVE402_PQ_FALCON_NETWORK",
            )
        }
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        os.environ.pop("LIVE402_PQ_LOG_VKEY", None)
        os.environ.pop("LIVE402_PQ_LOG_VKEY_MAINNET", None)
        os.environ.pop("LIVE402_PQ_LOG_EPOCH", None)
        os.environ.pop("LIVE402_PQ_FALCON_NETWORK", None)
        store.reset()
        receipt.configure_signer(None)

    def tearDown(self):
        receipt.configure_signer(None)
        store.reset()
        for key, val in self._prev.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self.tmp.cleanup()

    def test_env_vkey_wins_over_stale_sqlite(self):
        store.meta_set("vkey", "stale-sqlite-vkey")
        os.environ["LIVE402_PQ_LOG_VKEY"] = "env-vkey-wins"
        self.assertEqual(pq_view.public_vkey(), "env-vkey-wins")
        self.assertEqual(store.meta_get("vkey"), "stale-sqlite-vkey")

    def test_sqlite_used_only_when_env_empty(self):
        store.meta_set("vkey", "sqlite-only-vkey")
        os.environ.pop("LIVE402_PQ_LOG_VKEY", None)
        self.assertEqual(pq_view.public_vkey(), "sqlite-only-vkey")


class PaidRequireTransparencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._prev_db = os.environ.get("LIVE402_REPLAY_DB")
        self._prev_pq = os.environ.get("LIVE402_PQ_LOG_DB")
        os.environ["LIVE402_REPLAY_DB"] = os.path.join(self.tmp.name, "replay.sqlite")
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        replay.reset()
        store.reset()
        receipt.configure_signer(None)
        os.environ["CDP_ACCESS_TOKEN"] = "test-fixture-token"
        os.environ.pop("LOCAL_FREE", None)

    def tearDown(self):
        receipt.configure_signer(None)
        store.reset()
        replay.reset()
        os.environ.pop("CDP_ACCESS_TOKEN", None)
        if self._prev_db is None:
            os.environ.pop("LIVE402_REPLAY_DB", None)
        else:
            os.environ["LIVE402_REPLAY_DB"] = self._prev_db
        if self._prev_pq is None:
            os.environ.pop("LIVE402_PQ_LOG_DB", None)
        else:
            os.environ["LIVE402_PQ_LOG_DB"] = self._prev_pq
        self.tmp.cleanup()

    def _paid(self, body, nonce="s4"):
        headers = _headers_for(_payload(nonce, resource_url=ROUTE))
        with patch("live402.facilitator.post_json", side_effect=_fake_facilitator):
            return handle_route(body, headers, ROUTE)

    def test_paid_require_transparency_never_succeeds_logged_uncheckpointed(self):
        """Paid path with require_transparency never returns logged_uncheckpointed as success."""
        body = dict(_weather_body())
        body["require_transparency"] = True
        code, result, _extra = self._paid(body, nonce="rt")
        self.assertEqual(code, 503)
        self.assertIn("transparency", (result.get("error") or "").lower())
        tr = ((result.get("pq_trust") or {}).get("transparency") or {})
        self.assertEqual(tr.get("status"), "logged_uncheckpointed")
        self.assertNotEqual(code, 200)
        self.assertFalse(result.get("live"))

    def test_paid_200_allows_logged_uncheckpointed_by_default(self):
        code, result, _extra = self._paid(_weather_body(), nonce="ok")
        self.assertEqual(code, 200)
        self.assertTrue(result.get("live"))
        self.assertEqual(
            result["pq_trust"]["transparency"]["status"],
            "logged_uncheckpointed",
        )

    def test_paid_503_miss_allows_logged_uncheckpointed_by_default(self):
        code, result, _extra = self._paid(
            {"need": "echo", "url": "https://fixture.402signal.local/echo"},
            nonce="ms",
        )
        self.assertEqual(code, 503)
        self.assertFalse(result.get("live"))
        self.assertEqual(result.get("miss_reason"), "reachable_200")
        self.assertNotIn("transparency receipt", (result.get("error") or "").lower())
        self.assertEqual(
            result["pq_trust"]["transparency"]["status"],
            "logged_uncheckpointed",
        )

    def test_paid_503_miss_require_transparency_is_not_success(self):
        code, result, _extra = self._paid(
            {
                "need": "echo",
                "url": "https://fixture.402signal.local/echo",
                "require_transparency": True,
            },
            nonce="mt",
        )
        self.assertEqual(code, 503)
        self.assertIn("transparency", (result.get("error") or "").lower())
        tr = ((result.get("pq_trust") or {}).get("transparency") or {})
        self.assertEqual(tr.get("status"), "logged_uncheckpointed")
        self.assertFalse(result.get("live"))


if __name__ == "__main__":
    unittest.main()
