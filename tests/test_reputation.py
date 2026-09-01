"""PR16 reputation components + V1 score. No network."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import economics, history, payment, reputation, select
from tests.v2accept import attach_v2


def _hist(success_7d=None, n_7d=0):
    return {
        "success_7d": success_7d,
        "n_7d": n_7d,
        "n_24h": 0,
        "success_24h": None,
        "p50_latency_ms": None,
        "p95_latency_ms": None,
    }


def _default_payto(rail):
    if rail == "solana":
        return payment.DEFAULT_PAYTO_SOLANA
    if rail == "algorand":
        return payment.DEFAULT_PAYTO_ALGORAND
    return "0xabcabcabcabcabcabcabcabcabcabcabcabcabca"


def _hit(url="https://a.example/x", rail="base", history=None, **extra):
    pay_to = extra.pop("pay_to", None) or extra.get("payTo") or _default_payto(rail)
    amount = extra.pop("amount", 10000)
    row = {
        "url": url,
        "rail": rail,
        "live": True,
        "payTo": pay_to,
        "invocable": True,
        "latency_ms": extra.pop("latency", 10),
        "amount": amount,
        "asset": payment.usdc_asset_for_rail(rail) or payment.USDC_BASE,
        "history": history if history is not None else _hist(),
        "accepts": [
            {
                "network": (
                    payment.SOLANA_MAINNET
                    if rail == "solana"
                    else payment.ALGORAND_MAINNET
                    if rail == "algorand"
                    else payment.BASE_CAIP2
                ),
                "asset": payment.usdc_asset_for_rail(rail) or payment.USDC_BASE,
                "payTo": pay_to,
                "amount": amount,
            }
        ],
    }
    row.update(extra)
    return attach_v2(row)


class ComponentTests(unittest.TestCase):
    def test_components_without_score_flag(self):
        ev = {
            "n_7d": 12,
            "ok_7d": 11,
            "success_7d": 11 / 12,
            "probe_count_7d": 12,
            "distinct_days_7d": 4,
            "has_probe_history": True,
            "outcome_flips_7d": 1,
        }
        comps = reputation.components_from_evidence(ev, {"source_count": 2, "days_listed": 40, "first_seen": 1})
        self.assertNotIn("reputation_score", comps)
        self.assertEqual(comps["observed"]["success_count"], 11)
        self.assertEqual(comps["observed"]["observation_count"], 12)
        self.assertEqual(comps["observed"]["distinct_days_observed"], 4)
        self.assertEqual(comps["usage"]["probe_count"]["label"], "402signal_observed")
        self.assertEqual(comps["usage"]["probe_count"]["value"], 12)
        self.assertIsNone(comps["usage"]["settlement_count"]["value"])
        self.assertEqual(comps["usage"]["settlement_count"]["status"], "unknown")
        self.assertIsNone(comps["usage"]["unique_payer_count"]["value"])
        self.assertEqual(comps["source_count"], 2)
        self.assertEqual(comps["tenure"]["days_listed"], 40)

    def test_missing_usage_is_not_zero(self):
        empty = reputation.components_from_evidence({}, {})
        self.assertIsNone(empty["usage"]["probe_count"]["value"])
        self.assertEqual(empty["usage"]["probe_count"].get("status"), "unknown")
        self.assertIsNone(empty["usage"]["settlement_count"]["value"])
        self.assertIsNot(empty["usage"]["settlement_count"]["value"], 0)
        self.assertIsNone(empty["source_count"])
        self.assertIsNone(empty["tenure"]["days_listed"])
        self.assertIsNone(empty["observed"]["observation_count"])

    def test_no_payer_lists(self):
        dirty = reputation.components_from_evidence(
            {"n_7d": 5, "ok_7d": 5, "success_7d": 1.0, "has_probe_history": True, "probe_count_7d": 5},
            {},
        )
        blob = json.dumps(dirty)
        for banned in (
            "unique_payer_addresses",
            "payer_addresses",
            "payer_list",
            "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        ):
            self.assertNotIn(banned, blob)

    def test_settlement_not_faked_from_probes(self):
        ev = {
            "n_7d": 40,
            "ok_7d": 40,
            "success_7d": 1.0,
            "probe_count_7d": 40,
            "has_probe_history": True,
        }
        comps = reputation.components_from_evidence(ev, {})
        self.assertIsNone(comps["usage"]["settlement_count"]["value"])
        self.assertIn("no_settlement_ledger", comps["usage"]["settlement_count"]["reason"])


class ScoreTests(unittest.TestCase):
    def test_score_never_without_components(self):
        ev = {"n_7d": 20, "ok_7d": 18, "success_7d": 0.9, "probe_count_7d": 20, "has_probe_history": True}
        comps = reputation.components_from_evidence(ev, {"source_count": 1, "days_listed": 10})
        scored = reputation.score_v1(comps, ev)
        self.assertIn("observed", scored)
        self.assertIn("usage", scored)
        self.assertIsNotNone(scored["reputation_score"])
        self.assertIsNotNone(scored["reputation_confidence"])
        self.assertEqual(scored["scoring_model_id"], reputation.MODEL_ID)
        self.assertEqual(scored["scoring_model_hash"], reputation.model_hash())
        self.assertEqual(len(scored["scoring_model_hash"]), 64)

    def test_model_hash_is_canonical(self):
        spec = reputation.model_spec()
        digest = hashlib.sha256(
            json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        self.assertEqual(reputation.model_hash(), digest)
        self.assertFalse(spec.get("algo_bonus"))
        self.assertTrue(spec.get("chain_neutral"))

    def test_n_below_10_is_low_confidence_no_public_pct(self):
        ev = {"n_7d": 4, "ok_7d": 4, "success_7d": 1.0, "probe_count_7d": 4, "has_probe_history": True}
        scored = reputation.score_v1(reputation.components_from_evidence(ev, {}), ev)
        self.assertLessEqual(scored["reputation_confidence"], reputation.LOW_CONFIDENCE_CAP)
        self.assertIsNone(reputation.public_reliability_pct(4, 1.0))
        self.assertIsNone(reputation.public_reliability_pct(9, 1.0))
        self.assertEqual(reputation.public_reliability_pct(10, 0.9), 0.9)

    def test_popularity_cannot_dominate_observed(self):
        popular_weak = {
            "n_7d": 10000,
            "ok_7d": 5000,
            "success_7d": 0.5,
            "probe_count_7d": 10000,
            "has_probe_history": True,
        }
        quiet_strong = {
            "n_7d": 40,
            "ok_7d": 38,
            "success_7d": 0.95,
            "probe_count_7d": 40,
            "has_probe_history": True,
        }
        a = reputation.score_v1(reputation.components_from_evidence(popular_weak, {}), popular_weak)
        b = reputation.score_v1(reputation.components_from_evidence(quiet_strong, {}), quiet_strong)
        self.assertGreater(b["reputation_score"], a["reputation_score"])

    def test_unknown_usage_not_worse_than_zero_and_not_perfect(self):
        measured = {
            "n_7d": 20,
            "ok_7d": 18,
            "success_7d": 0.9,
            "probe_count_7d": 20,
            "has_probe_history": True,
        }
        listing = {"days_listed": 30, "source_count": 1}
        with_probes = reputation.score_v1(reputation.components_from_evidence(measured, listing), measured)
        zero_ev = {
            "n_7d": 20,
            "ok_7d": 18,
            "success_7d": 0.9,
            "probe_count_7d": 0,
            "has_probe_history": True,
        }
        unknown_ev = {
            "n_7d": 20,
            "ok_7d": 18,
            "success_7d": 0.9,
            "has_probe_history": True,
        }
        comps_zero = reputation.components_from_evidence(zero_ev, listing)
        comps_unknown = reputation.components_from_evidence(unknown_ev, listing)
        zero_scored = reputation.score_v1(comps_zero, zero_ev)
        unknown_scored = reputation.score_v1(comps_unknown, unknown_ev)
        # Missing is not scored as 0.0 and not as 1.0 (perfect). Both omit usage
        # so a never-measured peer is not worse than a measured-zero peer.
        self.assertIsNone(unknown_scored["scoring_components"]["values"]["usage"])
        self.assertIsNone(zero_scored["scoring_components"]["values"]["usage"])
        self.assertNotIn("usage", zero_scored["scoring_components"]["present"])
        self.assertNotIn("usage", unknown_scored["scoring_components"]["present"])
        self.assertGreaterEqual(unknown_scored["reputation_score"], zero_scored["reputation_score"])
        self.assertEqual(zero_scored["reputation_score"], unknown_scored["reputation_score"])
        self.assertLess(unknown_scored["reputation_score"], 1.0)
        self.assertIn("usage", with_probes["scoring_components"]["present"])
        self.assertGreater(with_probes["scoring_components"]["values"]["usage"], 0.0)
        self.assertLess(with_probes["scoring_components"]["values"]["usage"], 1.0)

    def test_same_score_on_all_rails(self):
        ev = {
            "n_7d": 20,
            "ok_7d": 16,
            "success_7d": 0.8,
            "probe_count_7d": 20,
            "has_probe_history": True,
        }
        listing = {"days_listed": 12, "source_count": 2}
        scores = []
        for rail in ("base", "solana", "algorand"):
            hit = _hit(url="https://%s.example/x" % rail, rail=rail, history=_hist(0.8, 20))
            scored = reputation.for_result(hit, listing=listing, evidence=ev)
            scores.append(scored["reputation_score"])
        self.assertEqual(scores[0], scores[1])
        self.assertEqual(scores[1], scores[2])

    def test_no_algo_bonus_in_code(self):
        """Ban ranking identifiers/assignments, not prose mentions in docs."""
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        banned = {"algo_bonus", "algo_multiplier", "algo_first"}
        for path in (root / "live402").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in banned:
                    # Allowed only as the documented denial in model_spec.
                    self.assertEqual(path.name, "reputation.py", msg=str(path))
                    self.assertEqual(node.id, "algo_bonus")
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in banned:
                            self.fail("ranking assignment %s in %s" % (target.id, path))
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    if node.target.id in banned:
                        self.fail("ranking assignment %s in %s" % (node.target.id, path))
                if isinstance(node, ast.Dict):
                    for key, val in zip(node.keys, node.values):
                        if not isinstance(key, ast.Constant) or key.value not in banned:
                            continue
                        if path.name == "reputation.py" and key.value == "algo_bonus":
                            self.assertIsInstance(val, ast.Constant)
                            self.assertIs(val.value, False)
                            continue
                        self.fail("ranking weight %r in %s" % (key.value, path))
        spec = reputation.model_spec()
        self.assertFalse(spec.get("algo_bonus"))
        self.assertNotIn("algo_bonus", reputation.WEIGHTS)


class HistoryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("LIVE402_HISTORY_DB")
        fd, self._path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.environ["LIVE402_HISTORY_DB"] = self._path
        history.reset()

    def tearDown(self):
        history.reset()
        if self._prev is None:
            os.environ.pop("LIVE402_HISTORY_DB", None)
        else:
            os.environ["LIVE402_HISTORY_DB"] = self._prev
        for p in (self._path, self._path + "-wal", self._path + "-shm"):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_evidence_and_model_log(self):
        url = "https://hist.example/rep"
        for i in range(12):
            history.record_probe(
                url,
                {
                    "live": True,
                    "status": 402,
                    "latency_ms": 10,
                    "payTo": "0xabc",
                    "amount": "10000",
                    "rail": "base",
                    "envelope": {
                        "x402Version": 2,
                        "accepts": [
                            {
                                "scheme": "exact",
                                "network": payment.BASE_CAIP2,
                                "asset": payment.USDC_BASE,
                                "amount": "10000",
                                "payTo": "0xabc",
                            }
                        ],
                    },
                },
            )
        ev = history.reputation_evidence(url)
        self.assertTrue(ev["has_probe_history"])
        self.assertEqual(ev["n_7d"], 12)
        self.assertEqual(ev["ok_7d"], 12)
        self.assertGreaterEqual(ev["distinct_days_7d"], 1)
        self.assertIsNone(ev.get("unique_payer_addresses"))
        result = {"url": url, "live": True, "history": history.summary(url)}
        history.attach_to_result(result)
        self.assertIn("reputation", result)
        self.assertIn("observed", result["reputation"])
        self.assertEqual(result["reputation"]["scoring_model_hash"], reputation.model_hash())
        logged = history.scoring_model(reputation.MODEL_ID)
        self.assertIsNotNone(logged)
        self.assertEqual(logged["model_hash"], reputation.model_hash())

    def test_never_seen_is_unknown_not_zero_usage(self):
        ev = history.reputation_evidence("https://never.example/x")
        self.assertFalse(ev["has_probe_history"])
        self.assertIsNone(ev["n_7d"])
        comps = reputation.components_from_evidence(ev, {})
        self.assertIsNone(comps["usage"]["probe_count"]["value"])
        scored = reputation.score_v1(comps, ev)
        self.assertIsNone(scored["reputation_score"])
        self.assertLessEqual(scored["reputation_confidence"], reputation.VERY_LOW_CONFIDENCE_CAP)


class ComparisonPrivacyTests(unittest.TestCase):
    def test_compared_has_reputation_and_no_payer_list(self):
        a = _hit(url="https://a.example/x", history=_hist(0.9, 12))
        b = _hit(url="https://b.example/x", history=_hist(0.5, 12), rail="solana")
        winner = select.pick_winner([a, b], "most_reliable", None)
        rows = select.comparison([a, b], winner)
        blob = json.dumps(rows)
        self.assertNotIn("unique_payer_addresses", blob)
        self.assertNotIn("payer_addresses", blob)
        self.assertIn("reputation", rows[0])
        self.assertIn("economics", rows[0])
        self.assertIn("observed", rows[0]["reputation"])
        if rows[0]["reputation"].get("reputation_score") is not None:
            self.assertTrue(rows[0]["reputation"].get("scoring_model_hash"))


if __name__ == "__main__":
    unittest.main()
