"""Settlement provenance isolation and trusted payTo rotation."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import history, payment, shadow


VALID_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
VALID_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _complete_envelope(pay_to, amount="10000"):
    return {
        "x402Version": 2,
        "accepts": [
            {
                "scheme": "exact",
                "network": payment.BASE_CAIP2,
                "asset": payment.USDC_BASE,
                "amount": amount,
                "payTo": pay_to,
                "maxTimeoutSeconds": 60,
            }
        ],
    }


def _snap(url, pay_to=VALID_A, live=True, **extra):
    ts = extra.pop("ts", int(time.time()))
    row = {
        "url": url,
        "live": bool(live),
        "status": 402 if live else None,
        "latency_ms": extra.pop("latency_ms", 10),
        "payTo": pay_to if live else None,
        "ts": ts,
        "rail": "base",
        "amount": extra.pop("amount", "10000"),
        "asset": payment.USDC_BASE,
        "envelope": _complete_envelope(pay_to if live else VALID_A, extra.get("amount", "10000") if False else "10000"),
    }
    row.update(extra)
    if live and row.get("payTo") and not row.get("envelope"):
        row["envelope"] = _complete_envelope(row["payTo"], row.get("amount") or "10000")
    if live:
        row["envelope"] = _complete_envelope(row["payTo"], row.get("amount") or "10000")
    return row


class SettlementProvenanceTests(unittest.TestCase):
    def setUp(self):
        self._prev_h = os.environ.get("LIVE402_HISTORY_DB")
        self._prev_c = os.environ.get("LIVE402_CATALOG_DB")
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_HISTORY_DB"] = os.path.join(self.tmp.name, "hist.sqlite")
        os.environ["LIVE402_CATALOG_DB"] = os.path.join(self.tmp.name, "cat.sqlite")
        history.reset()
        shadow.reset()

    def tearDown(self):
        history.reset()
        shadow.reset()
        if self._prev_h is None:
            os.environ.pop("LIVE402_HISTORY_DB", None)
        else:
            os.environ["LIVE402_HISTORY_DB"] = self._prev_h
        if self._prev_c is None:
            os.environ.pop("LIVE402_CATALOG_DB", None)
        else:
            os.environ["LIVE402_CATALOG_DB"] = self._prev_c
        self.tmp.cleanup()

    def _trust_class(self, url):
        conn = sqlite3.connect(os.environ["LIVE402_HISTORY_DB"])
        row = conn.execute(
            "SELECT trust_class, settled_route_observation FROM probes WHERE url = ? ORDER BY id DESC LIMIT 1",
            (url,),
        ).fetchone()
        conn.close()
        return row

    def test_persist_route_batch_is_tentative_and_skips_url_state(self):
        url = "https://prov.example/wx"
        t0 = int(time.time()) - 20
        history.record_probe(url, _snap(url, VALID_A, ts=t0))
        before = history.summary(url)
        self.assertEqual(before["last_payTo"], VALID_A)
        self.assertEqual(before["n_7d"], 1)
        bid = "tent1"
        later = _snap(url, VALID_B, ts=t0 + 5, batch_id=bid)
        metas = history.persist_route_batch(bid, [later])
        self.assertTrue(metas[url].get("payTo_pending") or metas[url].get("payTo_flipped"))
        after = history.summary(url)
        self.assertEqual(after["last_payTo"], VALID_A)
        self.assertEqual(after["last_checked"], before["last_checked"])
        self.assertEqual(after["n_7d"], 1)
        self.assertEqual(self._trust_class(url)[0], history.TRUST_ROUTE_TENTATIVE)
        hints = history.rank_hints([url])
        self.assertEqual(hints[url]["n_7d"], 1)
        prev = history.preview_observations([url])
        self.assertEqual(prev[url]["status"], "observed")
        ev = history.reputation_evidence(url)
        self.assertEqual(ev["n_7d"], 1)

    def test_failed_tentative_twice_does_not_establish_payto_b(self):
        url = "https://prov.example/twice"
        t0 = int(time.time()) - 30
        history.record_probe(url, _snap(url, VALID_A, ts=t0))
        history.persist_route_batch("tb1", [_snap(url, VALID_B, ts=t0 + 2, batch_id="tb1")])
        history.persist_route_batch("tb2", [_snap(url, VALID_B, ts=t0 + 4, batch_id="tb2")])
        summ = history.summary(url)
        self.assertEqual(summ["last_payTo"], VALID_A)
        conn = sqlite3.connect(os.environ["LIVE402_HISTORY_DB"])
        pending = conn.execute("SELECT pending_payTo FROM url_state WHERE url = ?", (url,)).fetchone()
        conn.close()
        self.assertIsNone(pending[0] if pending else None)

    def test_settled_b_after_trusted_a_is_pending(self):
        url = "https://prov.example/settle-b"
        t0 = int(time.time()) - 30
        history.record_probe(url, _snap(url, VALID_A, ts=t0))
        bid = "sb1"
        history.persist_route_batch(bid, [_snap(url, VALID_B, ts=t0 + 3, batch_id=bid)])
        history.mark_batch_settled(bid)
        summ = history.summary(url)
        self.assertEqual(summ["last_payTo"], VALID_A)
        conn = sqlite3.connect(os.environ["LIVE402_HISTORY_DB"])
        pending = conn.execute("SELECT pending_payTo FROM url_state WHERE url = ?", (url,)).fetchone()[0]
        trust = conn.execute(
            "SELECT trust_class FROM probes WHERE url = ? ORDER BY id DESC LIMIT 1", (url,)
        ).fetchone()[0]
        conn.close()
        self.assertTrue(payment.payto_equal(pending, VALID_B, "base"))
        self.assertEqual(trust, history.TRUST_ROUTE_SETTLED)
        ev = history.reputation_evidence(url)
        self.assertEqual(ev["n_7d"], 2)

    def test_later_independent_b_establishes(self):
        url = "https://prov.example/establish"
        t0 = int(time.time()) - 40
        history.record_probe(url, _snap(url, VALID_A, ts=t0))
        bid = "est1"
        history.persist_route_batch(bid, [_snap(url, VALID_B, ts=t0 + 2, batch_id=bid)])
        history.mark_batch_settled(bid)
        history.record_probe(url, _snap(url, VALID_B, ts=t0 + 6))
        self.assertTrue(payment.payto_equal(history.summary(url)["last_payTo"], VALID_B, "base"))

    def test_later_settled_b_establishes(self):
        url = "https://prov.example/settle-est"
        t0 = int(time.time()) - 40
        history.record_probe(url, _snap(url, VALID_A, ts=t0))
        history.persist_route_batch("s1", [_snap(url, VALID_B, ts=t0 + 2, batch_id="s1")])
        history.mark_batch_settled("s1")
        history.persist_route_batch("s2", [_snap(url, VALID_B, ts=t0 + 8, batch_id="s2")])
        history.mark_batch_settled("s2")
        self.assertTrue(payment.payto_equal(history.summary(url)["last_payTo"], VALID_B, "base"))

    def test_failed_settlement_does_not_mutate_url_state(self):
        url = "https://prov.example/fail-settle"
        t0 = int(time.time()) - 15
        history.record_probe(url, _snap(url, VALID_A, ts=t0))
        before = history.summary(url)
        history.persist_route_batch("fs1", [_snap(url, VALID_B, ts=t0 + 4, batch_id="fs1")])
        after = history.summary(url)
        self.assertEqual(after["last_payTo"], before["last_payTo"])
        self.assertEqual(after["last_checked"], before["last_checked"])
        self.assertEqual(after["n_7d"], 1)

    def test_late_settlement_does_not_overwrite_newer_trusted(self):
        url = "https://prov.example/late"
        t0 = int(time.time()) - 50
        history.record_probe(url, _snap(url, VALID_A, ts=t0, amount="10000"))
        old_batch = "late-old"
        history.persist_route_batch(
            old_batch, [_snap(url, VALID_B, ts=t0 + 1, batch_id=old_batch, amount="20000")]
        )
        history.record_probe(url, _snap(url, VALID_A, ts=t0 + 20, amount="10000"))
        newer = history.summary(url)
        history.mark_batch_settled(old_batch)
        after = history.summary(url)
        self.assertEqual(after["last_checked"], newer["last_checked"])
        self.assertEqual(after["last_payTo"], VALID_A)

    def test_old_row_classification_is_conservative(self):
        url = "https://prov.example/legacy"
        path = os.path.join(self.tmp.name, "legacy.sqlite")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE probes (id INTEGER PRIMARY KEY, url TEXT, ts INTEGER, live INTEGER, "
            "payable INTEGER, invocable INTEGER, latency_ms INTEGER, payTo TEXT, amount TEXT, "
            "miss_reason TEXT, rail TEXT, schema_present INTEGER, "
            "settled_route_observation INTEGER NOT NULL DEFAULT 1)"
        )
        conn.execute(
            "CREATE TABLE url_state (url TEXT PRIMARY KEY, last_payTo TEXT, last_amount TEXT, "
            "schema_present INTEGER, payTo_changed_at INTEGER, price_changed_at INTEGER, "
            "schema_changed_at INTEGER, last_checked INTEGER, last_success_402 INTEGER, "
            "pending_payTo TEXT)"
        )
        conn.execute(
            "CREATE TABLE observations (id INTEGER PRIMARY KEY, probe_id INTEGER, batch_id TEXT, "
            "source_type TEXT, source TEXT, rail TEXT, url TEXT, field TEXT, value TEXT, "
            "status TEXT, ts INTEGER)"
        )
        now = int(time.time())
        conn.execute(
            "INSERT INTO probes (url, ts, live, payable, invocable, latency_ms, payTo, amount, "
            "miss_reason, rail, schema_present, settled_route_observation) "
            "VALUES (?, ?, 1, 1, 0, 10, ?, '10000', NULL, 'base', NULL, 1)",
            (url, now, VALID_A),
        )
        conn.execute(
            "INSERT INTO probes (url, ts, live, payable, invocable, latency_ms, payTo, amount, "
            "miss_reason, rail, schema_present, settled_route_observation) "
            "VALUES (?, ?, 1, 1, 0, 11, ?, '10000', NULL, 'base', NULL, 0)",
            (url, now + 1, VALID_B),
        )
        conn.commit()
        conn.close()
        os.environ["LIVE402_HISTORY_DB"] = path
        # Re-open on the legacy file so additive migration classifies old rows.
        _ = history.summary(url)
        conn = sqlite3.connect(path)
        rows = conn.execute(
            "SELECT settled_route_observation, trust_class FROM probes WHERE url = ? ORDER BY id",
            (url,),
        ).fetchall()
        conn.close()
        self.assertEqual(rows[0][1], history.TRUST_INDEPENDENT)
        self.assertEqual(rows[1][1], history.TRUST_ROUTE_TENTATIVE)

    def test_tentative_does_not_touch_shadow_freshness(self):
        url = "https://prov.example/shadow"
        shadow.upsert_item(
            {
                "url": url,
                "accepts": [{"network": payment.BASE_CAIP2, "payTo": VALID_A, "amount": "10000", "asset": payment.USDC_BASE}],
                "_rail": "base",
            },
            source=shadow.SOURCE_CDP,
        )
        t0 = int(time.time()) - 10
        history.record_probe(url, _snap(url, VALID_A, ts=t0))
        trusted_verified = shadow.clocks(url).get("verification")
        with patch.object(shadow, "mark_verified") as marked:
            history.persist_route_batch("sh1", [_snap(url, VALID_B, ts=t0 + 3, batch_id="sh1")])
            marked.assert_not_called()
        self.assertEqual(shadow.clocks(url).get("verification"), trusted_verified)

    def test_mark_batch_settled_is_transactional_and_updates_shadow(self):
        url = "https://prov.example/tx"
        shadow.upsert_item(
            {
                "url": url,
                "accepts": [{"network": payment.BASE_CAIP2, "payTo": VALID_A, "amount": "10000", "asset": payment.USDC_BASE}],
                "_rail": "base",
            },
            source=shadow.SOURCE_CDP,
        )
        t0 = int(time.time()) - 12
        history.record_probe(url, _snap(url, VALID_A, ts=t0))
        bid = "tx1"
        history.persist_route_batch(bid, [_snap(url, VALID_A, ts=t0 + 4, batch_id=bid)])
        with patch.object(shadow, "mark_verified") as marked:
            history.mark_batch_settled(bid)
            self.assertTrue(marked.called)
        ev = history.reputation_evidence(url)
        self.assertEqual(ev["n_7d"], 2)

    def test_accept_payto_change_flag_is_read_only_on_tentative(self):
        url = "https://prov.example/optin"
        t0 = int(time.time()) - 8
        history.record_probe(url, _snap(url, VALID_A, ts=t0))
        later = _snap(url, VALID_B, ts=t0 + 2, batch_id="opt1")
        metas = history.persist_route_batch("opt1", [later])
        self.assertTrue(metas[url].get("payTo_pending"))
        self.assertEqual(history.summary(url)["last_payTo"], VALID_A)


if __name__ == "__main__":
    unittest.main()
