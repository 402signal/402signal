"""Transparent reputation evidence, then a documented V1 score.

Evidence is assembled first. A score is never returned without components.
Missing is not 0. Usage is not called reputation. Probe counts are not uptime.
Chain-neutral: the same function runs for Base, Solana, and Algorand.
There is no hidden Algorand preference.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone

# Weak public reliability stays hidden below this n. Same threshold as pulse.
MATURE_N = 10
WEAK_N = 3

# --- V1 scoring model (adjudicated; not a caller-defined slider) -------------
#
# Why these weights:
# - observed_performance 0.50: the only reliability-like signal 402Signal
#   actually measures (probe live/dead). Popularity and age must not outrank it.
# - stability 0.20: payTo / price / schema / rail churn is a risk signal we
#   persist. A recent payTo flip is more informative than an extra catalog hit.
# - tenure 0.10: days-listed is a weak prior. Age != quality. Log-capped.
# - usage 0.10: 402signal_observed probe counts only. Popularity != reliability.
#   Log-capped so one huge count cannot dominate. Settlement / unique payers
#   are omitted from the score because we do not have that ledger.
# - distribution 0.10: independent catalog sources are a weak corroboration
#   signal, not quality. Capped at 3 sources (the catalogs we federate).
#
# Missing component: neutralize (do not treat as 0 or as 1). The weight is
# dropped and reputation_confidence falls. A URL we have never scored on
# usage does not look worse than a peer with unknown usage, and does not
# look perfect either.
#
# Weak n (n_7d < 10): confidence is capped low. We still may emit a score
# next to components, but we do not present a mature public reliability %.
#
MODEL_ID = "reputation-v1"
MODEL_EFFECTIVE_TS = 1756627200  # 2026-08-31T00:00:00Z
WEIGHTS = {
    "observed_performance": 0.50,
    "stability": 0.20,
    "tenure": 0.10,
    "usage": 0.10,
    "distribution": 0.10,
}
USAGE_LOG_CAP = 100  # log1p(n) / log1p(100) saturates; 10k probes ~= 100
TENURE_LOG_CAP_DAYS = 365
SOURCE_CAP = 3
NEUTRAL = 0.5
LOW_CONFIDENCE_CAP = 0.35  # n_7d < MATURE_N
VERY_LOW_CONFIDENCE_CAP = 0.15  # n_7d < WEAK_N or no observed window

_PAYER_LIST_KEYS = (
    "unique_payer_addresses",
    "payer_addresses",
    "payers",
    "payer_list",
    "wallets",
    "addresses",
)


def _as_int(val):
    if val is None or val == "" or isinstance(val, bool):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _as_float(val):
    if val is None or val == "" or isinstance(val, bool):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _text(val) -> str | None:
    if val is None:
        return None
    text = str(val).strip()
    return text or None


def _iso_ts(ts) -> str | None:
    n = _as_int(ts)
    if n is None:
        return None
    return datetime.fromtimestamp(n, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _log_cap(n: float, cap: float) -> float:
    if n <= 0 or cap <= 0:
        return 0.0
    return min(1.0, math.log1p(n) / math.log1p(cap))


def model_spec() -> dict:
    """Canonical V1 methodology. Historical scores stay interpretable via hash."""
    return {
        "model_id": MODEL_ID,
        "effective_ts": MODEL_EFFECTIVE_TS,
        "scale": "0-1",
        "chain_neutral": True,
        "algo_bonus": False,
        "components": {
            "observed_performance": {
                "inputs": ["success_7d", "n_7d"],
                "rule": "success_7d when n_7d >= 3, else missing",
                "missing": "drop and lower confidence; never treat as 0.0",
            },
            "stability": {
                "inputs": [
                    "payTo_changed_at",
                    "price_changed_at",
                    "schema_changed_at",
                    "rail_changed_at",
                    "has_observation_history",
                ],
                "rule": (
                    "1.0 minus 0.35 per identity-class change (payTo, rail) "
                    "and 0.15 per quote-class change (price, schema) in the last 7d. "
                    "Floor 0. Unknown unless we have any observation history."
                ),
                "missing": "drop; never treat never-seen as perfectly stable",
            },
            "tenure": {
                "inputs": ["days_listed"],
                "rule": "log1p(days_listed) / log1p(365), cap 1.0",
                "missing": "drop; age is not quality",
            },
            "usage": {
                "inputs": ["probe_count_7d"],
                "label": "402signal_observed",
                "rule": "log1p(probe_count) / log1p(100), cap 1.0, only when count >= 1",
                "missing": (
                    "drop. 0 probes and unknown usage both omit this component "
                    "so 0 does not look worse than unknown. Settlement and "
                    "unique payers are never faked from probes."
                ),
            },
            "distribution": {
                "inputs": ["source_count"],
                "rule": "min(source_count, 3) / 3 when source_count is known",
                "missing": "drop; not in catalog != 0 sources",
            },
        },
        "weights": dict(WEIGHTS),
        "caps": {
            "usage_log_cap": USAGE_LOG_CAP,
            "tenure_log_cap_days": TENURE_LOG_CAP_DAYS,
            "source_cap": SOURCE_CAP,
        },
        "normalization": "weighted mean over present components only",
        "logarithmic_transforms": ["usage", "tenure"],
        "missing_data": (
            "Missing component is dropped (neutralized), not scored as 0 or 1. "
            "reputation_confidence is the present-weight fraction, then capped "
            "low when n_7d < 10."
        ),
        "confidence": {
            "present_weight_fraction": True,
            "n_7d_lt_3_cap": VERY_LOW_CONFIDENCE_CAP,
            "n_7d_lt_10_cap": LOW_CONFIDENCE_CAP,
            "no_public_reliability_pct_below_n": MATURE_N,
        },
        "privacy": {
            "unique_payer_addresses": "count only if we have identities; we do not",
            "payer_lists": "never emitted",
        },
    }


def model_hash() -> str:
    blob = json.dumps(model_spec(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def model_record() -> dict:
    spec = model_spec()
    digest = model_hash()
    return {
        "model_id": MODEL_ID,
        "model_hash": digest,
        "effective_ts": MODEL_EFFECTIVE_TS,
        "spec_json": json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
    }


def strip_payer_lists(obj):
    """Never emit payer address lists. Count-only fields may remain."""
    if isinstance(obj, dict):
        out = {}
        for key, val in obj.items():
            if key in _PAYER_LIST_KEYS:
                continue
            if isinstance(val, list) and key.endswith("_addresses"):
                continue
            out[key] = strip_payer_lists(val)
        return out
    if isinstance(obj, list):
        return [strip_payer_lists(x) for x in obj]
    return obj


def _unknown_usage_field(reason: str) -> dict:
    return {"value": None, "status": "unknown", "reason": reason, "label": "not_402signal_observed"}


def components_from_evidence(evidence: dict | None, listing: dict | None = None) -> dict:
    """Build the public components object. No score here."""
    ev = evidence if isinstance(evidence, dict) else {}
    listing = listing if isinstance(listing, dict) else {}
    n_7d = _as_int(ev.get("n_7d"))
    ok_7d = _as_int(ev.get("ok_7d"))
    has_window = ev.get("has_probe_history") is True or (n_7d is not None)
    observed = {}
    if has_window and n_7d is not None:
        observed["success_count"] = ok_7d if ok_7d is not None else 0
        observed["observation_count"] = n_7d
        days = _as_int(ev.get("distinct_days_7d"))
        if days is not None:
            observed["distinct_days_observed"] = days
        last_checked = ev.get("last_checked")
        last_ok = ev.get("last_success_402")
        freshness = {
            "last_checked": _iso_ts(last_checked) if last_checked is not None else None,
            "last_success_402": _iso_ts(last_ok) if last_ok is not None else None,
            "provenance": "402signal_observed",
        }
        age = _as_int(ev.get("age_s"))
        if age is None and last_checked is not None:
            age = max(0, int(time.time()) - int(last_checked))
        if age is not None:
            freshness["age_s"] = age
        observed["freshness"] = freshness
        flips = _as_int(ev.get("outcome_flips_7d"))
        rate = _as_float(ev.get("success_7d"))
        stability = {"window": "7d", "provenance": "402signal_observed"}
        if n_7d >= WEAK_N and rate is not None:
            stability["success_7d"] = rate
        else:
            stability["success_7d"] = None
        if flips is not None:
            stability["outcome_flips"] = flips
        observed["outcome_stability"] = stability
    else:
        observed = {
            "success_count": None,
            "observation_count": None,
            "distinct_days_observed": None,
            "freshness": None,
            "outcome_stability": None,
        }

    usage = {
        "settlement_count": _unknown_usage_field("no_settlement_ledger"),
        "unique_payer_count": _unknown_usage_field("no_payer_identities"),
    }
    probes = _as_int(ev.get("probe_count_7d"))
    if probes is None:
        probes = n_7d
    if has_window and probes is not None:
        usage["probe_count"] = {
            "value": probes,
            "window": "7d",
            "label": "402signal_observed",
            "note": "routing observations, not uptime and not reputation",
        }
    else:
        usage["probe_count"] = {
            "value": None,
            "status": "unknown",
            "label": "402signal_observed",
        }

    first_probe = ev.get("first_probe_ts")
    first_listed = listing.get("first_seen")
    first_seen = None
    first_prov = None
    if first_listed is not None and first_probe is not None:
        if int(first_listed) <= int(first_probe):
            first_seen, first_prov = int(first_listed), "catalog_claimed"
        else:
            first_seen, first_prov = int(first_probe), "402signal_observed"
    elif first_listed is not None:
        first_seen, first_prov = int(first_listed), "catalog_claimed"
    elif first_probe is not None:
        first_seen, first_prov = int(first_probe), "402signal_observed"
    days_listed = _as_int(listing.get("days_listed"))
    if days_listed is None and first_seen is not None and first_prov == "catalog_claimed":
        days_listed = max(0, int((int(time.time()) - first_seen) / 86400))
    tenure = {
        "first_seen": _iso_ts(first_seen),
        "first_seen_provenance": first_prov,
        "days_listed": days_listed,
    }

    def _change(changed_at, count_key):
        row = {"changed_at": _iso_ts(changed_at) if changed_at is not None else None}
        nchg = _as_int(ev.get(count_key))
        if nchg is not None:
            row["count"] = nchg
        elif has_window:
            row["count"] = 1 if changed_at is not None else 0
        else:
            row["count"] = None
        return row

    stability = {
        "payTo_changes": _change(ev.get("payTo_changed_at"), "payTo_change_count"),
        "price_changes": _change(ev.get("price_changed_at"), "price_change_count"),
        "schema_changes": _change(ev.get("schema_changed_at"), "schema_change_count"),
        "rail_changes": _change(ev.get("rail_changed_at"), "rail_change_count"),
    }

    source_count = _as_int(listing.get("source_count"))
    rails = ev.get("rails_observed")
    if not rails:
        rails = listing.get("claimed_rails")
    out = {
        "observed": observed,
        "usage": usage,
        "tenure": tenure,
        "stability": stability,
        "source_count": source_count,
    }
    if isinstance(rails, (list, tuple)) and rails:
        out["supported_rails"] = [str(r) for r in rails if r]
    return strip_payer_lists(out)


def evidence_from_result(result: dict | None) -> dict:
    """Build a minimal evidence dict from an in-memory result (no extra sqlite)."""
    if not isinstance(result, dict):
        return {}
    hist = result.get("history") if isinstance(result.get("history"), dict) else {}
    changes = result.get("changes") if isinstance(result.get("changes"), dict) else {}
    n_7d = _as_int(hist.get("n_7d"))
    ok = None
    rate = _as_float(hist.get("success_7d"))
    if n_7d is not None and rate is not None:
        ok = int(round(rate * n_7d))
    ev = {
        "n_7d": n_7d if n_7d is not None else None,
        "ok_7d": ok,
        "success_7d": rate,
        "probe_count_7d": n_7d,
        "has_probe_history": n_7d is not None,
        "last_checked": None,
        "last_success_402": None,
        "payTo_changed_at": changes.get("payTo_changed_at"),
        "price_changed_at": changes.get("price_changed_at"),
        "schema_changed_at": changes.get("schema_changed_at"),
        "rail_changed_at": changes.get("rail_changed_at"),
    }
    return ev


def _recent(ts, now: int, window: int = 86400 * 7) -> bool:
    n = _as_int(ts)
    if n is None:
        return False
    # ISO strings from attach_to_result changes{} are not unix; treat as present/recent unknown
    if isinstance(ts, str) and not str(ts).isdigit():
        return True
    return (now - n) <= window


def _component_scores(components: dict, evidence: dict) -> dict:
    """Return {name: float|None} for V1. None = missing (neutralize)."""
    ev = evidence if isinstance(evidence, dict) else {}
    n_7d = _as_int(ev.get("n_7d"))
    rate = None
    observed = components.get("observed") if isinstance(components.get("observed"), dict) else {}
    stab = observed.get("outcome_stability") if isinstance(observed.get("outcome_stability"), dict) else {}
    rate = _as_float(stab.get("success_7d"))
    if rate is None:
        rate = _as_float(ev.get("success_7d"))
    observed_score = None
    if n_7d is not None and n_7d >= WEAK_N and rate is not None:
        observed_score = max(0.0, min(1.0, rate))

    has_hist = ev.get("has_probe_history") is True or (n_7d is not None)
    stability_score = None
    if has_hist:
        now = int(time.time())
        st = components.get("stability") if isinstance(components.get("stability"), dict) else {}
        score = 1.0
        for key, penalty in (
            ("payTo_changes", 0.35),
            ("rail_changes", 0.35),
            ("price_changes", 0.15),
            ("schema_changes", 0.15),
        ):
            row = st.get(key) if isinstance(st.get(key), dict) else {}
            count = _as_int(row.get("count"))
            changed_at = row.get("changed_at") or ev.get(key.replace("changes", "changed_at"))
            recent = False
            if count and count > 0:
                recent = _recent(changed_at, now) if changed_at is not None else True
            elif changed_at is not None:
                recent = _recent(changed_at, now)
            if recent:
                score -= penalty
        stability_score = max(0.0, min(1.0, score))

    tenure_score = None
    tenure = components.get("tenure") if isinstance(components.get("tenure"), dict) else {}
    days = _as_int(tenure.get("days_listed"))
    if days is not None:
        tenure_score = _log_cap(float(days), float(TENURE_LOG_CAP_DAYS))

    usage_score = None
    usage = components.get("usage") if isinstance(components.get("usage"), dict) else {}
    probe = usage.get("probe_count") if isinstance(usage.get("probe_count"), dict) else {}
    probes = _as_int(probe.get("value"))
    if probes is not None and probes >= 1:
        usage_score = _log_cap(float(probes), float(USAGE_LOG_CAP))

    dist_score = None
    src = _as_int(components.get("source_count"))
    if src is not None:
        dist_score = min(float(SOURCE_CAP), max(0.0, float(src))) / float(SOURCE_CAP)

    return {
        "observed_performance": observed_score,
        "stability": stability_score,
        "tenure": tenure_score,
        "usage": usage_score,
        "distribution": dist_score,
    }


def score_v1(components: dict, evidence: dict | None = None) -> dict:
    """Deterministic V1. Never returns a score without the same components object."""
    comps = strip_payer_lists(components if isinstance(components, dict) else {})
    ev = evidence if isinstance(evidence, dict) else {}
    parts = _component_scores(comps, ev)
    present = {k: v for k, v in parts.items() if v is not None}
    dropped = [k for k, v in parts.items() if v is None]
    total_w = 0.0
    acc = 0.0
    for name, val in present.items():
        w = float(WEIGHTS.get(name) or 0)
        acc += val * w
        total_w += w
    score = (acc / total_w) if total_w > 0 else None
    present_frac = total_w / sum(WEIGHTS.values()) if WEIGHTS else 0.0
    n_7d = _as_int(ev.get("n_7d"))
    if n_7d is None:
        n_7d = _as_int((comps.get("observed") or {}).get("observation_count"))
    confidence = present_frac
    if n_7d is None or n_7d < WEAK_N:
        confidence = min(confidence, VERY_LOW_CONFIDENCE_CAP)
    elif n_7d < MATURE_N:
        confidence = min(confidence, LOW_CONFIDENCE_CAP)
    if score is not None:
        score = round(float(score), 6)
    confidence = round(float(confidence), 6)
    digest = model_hash()
    out = dict(comps)
    out["reputation_score"] = score
    out["reputation_confidence"] = confidence
    out["scoring_model_id"] = MODEL_ID
    out["scoring_model_hash"] = digest
    out["scoring_components"] = {
        "present": sorted(present.keys()),
        "dropped": dropped,
        "values": {k: (round(v, 6) if v is not None else None) for k, v in parts.items()},
    }
    return strip_payer_lists(out)


def public_reliability_pct(n_7d, success_7d):
    """Mature public % only. None below n=10. Never invent 0%."""
    n = _as_int(n_7d)
    rate = _as_float(success_7d)
    if n is None or n < MATURE_N or rate is None:
        return None
    return rate


def for_result(result: dict | None, *, listing=None, evidence=None, score: bool = True) -> dict:
    """Components, and V1 score+model when score=True. Never a payer list."""
    ev = evidence if isinstance(evidence, dict) else evidence_from_result(result)
    listing = listing if isinstance(listing, dict) else {}
    comps = components_from_evidence(ev, listing)
    if not score:
        return comps
    return score_v1(comps, ev)


def attach(result: dict | None, *, score: bool = True) -> dict:
    """Attach reputation on a probe/route result. Never raises."""
    if not isinstance(result, dict):
        return {}
    try:
        url = _text(result.get("url"))
        ev = None
        listing = {}
        if url:
            try:
                from live402 import history as history_mod

                ev = history_mod.reputation_evidence(url)
            except Exception:
                ev = None
            try:
                from live402 import shadow

                listing = shadow.listing_facts(url)
            except Exception:
                listing = {}
        if not ev:
            ev = evidence_from_result(result)
        payload = for_result(result, listing=listing, evidence=ev, score=score)
        result["reputation"] = payload
        if score:
            try:
                from live402 import history as history_mod

                history_mod.ensure_scoring_model(model_record())
            except Exception:
                pass
        return result
    except Exception:
        result.setdefault("reputation", components_from_evidence({}, {}))
        return result


def of(result: dict | None) -> dict | None:
    if not isinstance(result, dict):
        return None
    rep = result.get("reputation")
    if isinstance(rep, dict) and "observed" in rep:
        return rep
    return for_result(result)


def score_of(result: dict | None):
    rep = of(result)
    if not isinstance(rep, dict):
        return None
    return _as_float(rep.get("reputation_score"))


def confidence_of(result: dict | None):
    rep = of(result)
    if not isinstance(rep, dict):
        return None
    return _as_float(rep.get("reputation_confidence"))


def observed_success_of(result: dict | None):
    """success_7d when n >= 3. None is unknown, never 0.0."""
    if not isinstance(result, dict):
        return None
    hist = result.get("history") if isinstance(result.get("history"), dict) else {}
    n = _as_int(hist.get("n_7d"))
    if n is None or n < WEAK_N:
        return None
    return _as_float(hist.get("success_7d"))


def public_row(result: dict | None) -> dict | None:
    """Slim reputation object for compared[] / preview. Score only with components."""
    rep = of(result)
    if not isinstance(rep, dict):
        return None
    return strip_payer_lists(rep)
