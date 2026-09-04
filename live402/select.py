"""Constraint-aware best-of-N selection. Rail-neutral PRE-PAYMENT SIGNAL. Fail closed. Never pay.

networks is a hard policy lock: a HTTP 200 winner must carry selected_payment
from the CURRENT observed HTTP 402, and that option's rail must be in
request.networks (same rail_of_network matching the rest of the product).
Catalog claims never satisfy networks and never fill selected_payment.

prefer_network is a weak ranking preference only. It does not restrict
discovery or selection. Do not treat it as a filter.

cheapest / fastest / most_reliable rank the probed survivor set (currently
probed eligible candidates), not every discovered endpoint. fastest uses
this-request probe RTT (latency_ms), not settlement latency.
fastest_settlement is a separate objective.
"""

from __future__ import annotations

import math
from functools import cmp_to_key

from live402 import economics, payment, reputation

OBJECTIVES = (
    "best",
    "cheapest",
    "fastest",
    "most_reliable",
    "lowest_total_cost",
    "fastest_settlement",
)
RAILS = frozenset(("base", "solana", "algorand"))
WEAK_MIN_N = 3
MATURE_N = 10
COMPARED_CAP = 5
# Keys we still cannot compute. Empty in this slice: settlement/reputation/success
# are measured when data exists and fail closed when unknown.
UNMEASURED_CONSTRAINTS = ()
PREFER_NETWORKS = frozenset(("base", "solana", "algorand"))
SEARCH_DEPTHS = frozenset(("standard", "thorough"))
EXPLICIT_CONSTRAINT_KEYS = (
    "objective",
    "prefer_network",
    "networks",
    "max_amount_atomic",
    "max_price_usd",
    "max_latency_ms",
    "max_probe_latency_ms",
    "max_service_latency_ms",
    "require_invocable",
    "min_observations",
    "min_observed_success",
    "min_reputation_score",
    "min_reputation_confidence",
    "max_total_cost_usd",
    "max_settlement_latency_ms",
    "search_depth",
    "max_candidates_to_probe",
    "accept_payTo_change",
    "require_transparency",
)


class ConstraintError(ValueError):
    """Explicit structured constraint is malformed. HTTP 400, never weaken."""


def parse_objective(raw) -> str:
    """Unknown / missing → best. Do not 400 here."""
    if raw is None:
        return "best"
    text = str(raw).strip().lower()
    if text in OBJECTIVES:
        return text
    return "best"


def _nonneg_int(val):
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if val >= 0 else None
    if isinstance(val, str):
        text = val.strip()
        if not text:
            return None
        if text[0] == "+":
            text = text[1:]
        if text.isdigit():
            n = int(text)
            return n if n >= 0 else None
        return None
    return None


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


def _truthy(val, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        low = val.strip().lower()
        if low in {"1", "true", "yes"}:
            return True
        if low in {"0", "false", "no", ""}:
            return False
        return default
    if isinstance(val, int):
        return val != 0
    return default


def _text(val) -> str | None:
    if val is None:
        return None
    text = str(val).strip()
    return text or None


def _parse_rails(raw):
    """Missing → unrestricted (None). Explicit empty/invalid → empty set, never all rails."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = list(raw)
    else:
        return frozenset()
    rails = []
    for item in items:
        name = _text(item)
        if not name:
            continue
        name = name.lower()
        if name in RAILS:
            rails.append(name)
    return frozenset(rails)


def _caller_set(src: dict, key: str) -> bool:
    if key not in src:
        return False
    val = src.get(key)
    if val is None or val == "":
        return False
    return True


def _explicit_present(src: dict, key: str) -> bool:
    """Key present (including null) is explicit. Absent keys are unconstrained."""
    return isinstance(src, dict) and key in src


def _reject(message: str) -> None:
    raise ConstraintError(message)


def _require_bool(val, name: str) -> None:
    if not isinstance(val, bool):
        _reject("%s must be a boolean" % name)


def _require_int(val, name: str) -> int:
    if isinstance(val, bool) or not isinstance(val, int):
        _reject("%s must be an integer" % name)
    try:
        if val.bit_length() > 63:
            _reject("%s is out of range" % name)
    except (OverflowError, ValueError):
        _reject("%s is out of range" % name)
    if val < 0:
        _reject("%s cannot be negative" % name)
    return val


def _require_finite_number(val, name: str) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        _reject("%s must be a number" % name)
    n = float(val)
    if not math.isfinite(n):
        _reject("%s must be a finite number" % name)
    return n


def _require_probability(val, name: str) -> float:
    n = _require_finite_number(val, name)
    if n < 0 or n > 1:
        _reject("%s must be between 0 and 1" % name)
    return n


def _require_nonneg_number(val, name: str) -> float:
    n = _require_finite_number(val, name)
    if n < 0:
        _reject("%s cannot be negative" % name)
    return n


def _parse_explicit_networks(raw, name: str = "networks"):
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        _reject("%s must be a nonempty list or string of supported rails" % name)
        return []
    if not items:
        _reject("%s must be a nonempty list or string of supported rails" % name)
    rails: list[str] = []
    for item in items:
        if not isinstance(item, str):
            _reject("%s entries must be strings" % name)
        name_s = item.strip().lower()
        if name_s not in RAILS:
            _reject("unsupported network")
        if name_s not in rails:
            rails.append(name_s)
    if not rails:
        _reject("%s must be a nonempty list or string of supported rails" % name)
    return rails


def validate_explicit_constraints(body: dict) -> None:
    """HTTP 400 gate. Explicit malformed controls must not silently weaken.

    Natural-language policy is not validated here; unresolved phrases stay
    unresolved and are never guessed.
    """
    src = body if isinstance(body, dict) else {}
    # The public request schema keeps structured controls at the top level.
    # A nested container is especially dangerous to ignore: callers may
    # reasonably believe a price, network, or latency bound was enforced.
    # Refuse every value (including an empty object) instead of introducing
    # precedence rules or silently routing without the requested policy.
    if "constraints" in src:
        _reject("constraints must be specified as top-level fields")
    if _explicit_present(src, "objective"):
        raw = src.get("objective")
        if not isinstance(raw, str) or not raw.strip():
            _reject("unsupported objective")
        if raw.strip().lower() not in OBJECTIVES:
            _reject("unsupported objective")
    if _explicit_present(src, "prefer_network"):
        raw = src.get("prefer_network")
        if not isinstance(raw, str) or raw.strip().lower() not in PREFER_NETWORKS:
            _reject("unsupported prefer_network")
    if _explicit_present(src, "networks"):
        _parse_explicit_networks(src.get("networks"))
    for key in (
        "max_amount_atomic",
        "max_latency_ms",
        "max_probe_latency_ms",
        "max_service_latency_ms",
        "min_observations",
        "max_settlement_latency_ms",
    ):
        if _explicit_present(src, key):
            _require_int(src.get(key), key)
    if _explicit_present(src, "max_candidates_to_probe"):
        n = _require_int(src.get("max_candidates_to_probe"), "max_candidates_to_probe")
        if n < 1:
            _reject("max_candidates_to_probe must be at least 1")
    if _explicit_present(src, "require_invocable"):
        _require_bool(src.get("require_invocable"), "require_invocable")
    if _explicit_present(src, "accept_payTo_change"):
        _require_bool(src.get("accept_payTo_change"), "accept_payTo_change")
    if _explicit_present(src, "require_transparency"):
        _require_bool(src.get("require_transparency"), "require_transparency")
    if _explicit_present(src, "max_price_usd"):
        _require_nonneg_number(src.get("max_price_usd"), "max_price_usd")
    if _explicit_present(src, "max_total_cost_usd"):
        _require_nonneg_number(src.get("max_total_cost_usd"), "max_total_cost_usd")
    if _explicit_present(src, "min_reputation_score"):
        _require_probability(src.get("min_reputation_score"), "min_reputation_score")
    if _explicit_present(src, "min_reputation_confidence"):
        _require_probability(src.get("min_reputation_confidence"), "min_reputation_confidence")
    if _explicit_present(src, "min_observed_success"):
        _require_probability(src.get("min_observed_success"), "min_observed_success")
    if _explicit_present(src, "search_depth"):
        raw = src.get("search_depth")
        if not isinstance(raw, str) or raw.strip().lower() not in SEARCH_DEPTHS:
            _reject("unsupported search_depth")


def parse_constraints(body: dict) -> dict:
    """Normalize caller constraints. Invalid numeric bounds → unconstrained (None).

    max_latency_ms is a compatibility alias for max_probe_latency_ms (this
    request's probe RTT). max_service_latency_ms is historical p50 only —
    never mixed with probe latency. max_settlement_latency_ms is
    settlement/finality, never probe RTT. max_total_cost_usd is merchant
    price plus known fees; unknown fee fails closed. Unknown measured
    values fail closed when the caller set the bound.
    """
    src = body if isinstance(body, dict) else {}
    networks = src.get("networks")
    if networks is None:
        networks = src.get("rails")
    max_usd = _as_float(src.get("max_price_usd"))
    if max_usd is not None and max_usd < 0:
        max_usd = None
    max_total = _as_float(src.get("max_total_cost_usd"))
    if max_total is not None and max_total < 0:
        max_total = None
    min_rep = _as_float(src.get("min_reputation_score"))
    if min_rep is not None and min_rep < 0:
        min_rep = None
    min_success = _as_float(src.get("min_observed_success"))
    if min_success is not None and (min_success < 0 or min_success > 1):
        min_success = None
    min_conf = _as_float(src.get("min_reputation_confidence"))
    if min_conf is not None and (min_conf < 0 or min_conf > 1):
        min_conf = None
    max_lat = _nonneg_int(src.get("max_latency_ms"))
    max_probe = _nonneg_int(src.get("max_probe_latency_ms"))
    if max_probe is None:
        max_probe = max_lat
    unmeasured = tuple(key for key in UNMEASURED_CONSTRAINTS if _caller_set(src, key))
    return {
        "max_amount_atomic": _nonneg_int(src.get("max_amount_atomic")),
        "max_price_usd": max_usd,
        "max_total_cost_usd": max_total,
        "max_latency_ms": max_lat if max_lat is not None else max_probe,
        "max_probe_latency_ms": max_probe,
        "max_service_latency_ms": _nonneg_int(src.get("max_service_latency_ms")),
        "max_settlement_latency_ms": _nonneg_int(src.get("max_settlement_latency_ms")),
        "require_invocable": _truthy(src.get("require_invocable"), False),
        "rails": _parse_rails(networks),
        "min_observations": _nonneg_int(src.get("min_observations")),
        "min_observed_success": min_success,
        "min_reputation_score": min_rep,
        "min_reputation_confidence": min_conf,
        "unmeasured": unmeasured,
        "accept_payTo_change": _truthy(src.get("accept_payTo_change"), False),
        "require_transparency": _truthy(src.get("require_transparency"), False),
    }


def _amount_from_accepts(accepts) -> int | None:
    if not isinstance(accepts, list):
        return None
    for acc in accepts:
        if not isinstance(acc, dict):
            continue
        raw = acc.get("amount")
        if raw is None or raw == "":
            raw = acc.get("maxAmountRequired")
        n = _as_int(raw)
        if n is not None:
            return n
    return None


def payment_options(result) -> list[dict]:
    return payment.payment_options_from_result(result)


def amount_atomic(result) -> int | None:
    """Known atomic amount only. Never invent 0 for a missing price."""
    if not isinstance(result, dict):
        return None
    n = _as_int(result.get("amount"))
    if n is not None:
        return n
    n = _as_int(result.get("amountAtomic"))
    if n is not None:
        return n
    target = result.get("target") if isinstance(result.get("target"), dict) else {}
    n = _as_int(target.get("amountAtomic"))
    if n is not None:
        return n
    n = _amount_from_accepts(target.get("accepts"))
    if n is not None:
        return n
    hist = result.get("history") if isinstance(result.get("history"), dict) else {}
    for key in ("amountAtomic", "amount", "last_amount"):
        n = _as_int(hist.get(key))
        if n is not None:
            return n
    env = result.get("envelope") if isinstance(result.get("envelope"), dict) else {}
    n = _amount_from_accepts(env.get("accepts"))
    if n is not None:
        return n
    opts = payment_options(result)
    for opt in opts:
        n = _as_int(opt.get("amount_atomic"))
        if n is not None:
            return n
    return None


def _result_rails(result) -> set:
    """Observed payment-option rails only. Catalog result.rail must not count."""
    rails: set = set()
    for opt in payment_options(result):
        if opt.get("rail") in RAILS:
            rails.add(opt["rail"])
    return rails


def _complete_options_for_constraints(result, cons) -> list[dict]:
    """Complete CURRENT observed options that survive network + price bounds."""
    env = result.get("envelope") if isinstance(result, dict) and isinstance(result.get("envelope"), dict) else {}
    return [
        o
        for o in _options_for_constraints(result, cons)
        if payment.is_complete_payment_option(o, env)
    ]


def selected_payment_is_complete(selected) -> bool:
    """True iff selected_payment is a complete observed option. Never invent fields."""
    if not isinstance(selected, dict):
        return False
    rail = selected.get("rail")
    if rail not in RAILS:
        return False
    network = selected.get("network")
    if not network or payment.rail_of_network(network) != rail:
        return False
    if selected.get("amount_atomic") is None:
        return False
    if not selected.get("asset"):
        return False
    if not selected.get("payTo"):
        return False
    return True


def selected_payment_matches_networks(selected, constraints) -> bool:
    """selected_payment.network must be in request.networks when that lock is set."""
    rails = constraints.get("rails") if isinstance(constraints, dict) else None
    if not isinstance(rails, frozenset):
        return True
    if not selected_payment_is_complete(selected):
        return False
    rail = selected.get("rail") or payment.rail_of_network(selected.get("network"))
    return rail in rails


def http200_winner_ok(result, objective=None, constraints=None) -> bool:
    """HTTP 200 requires a live payable winner plus the exact observed option.

    selected_payment must be CURRENT observed 402 (never catalog). When
    networks is a nonempty lock, that option's rail must be in the lock.
    """
    if not isinstance(result, dict) or not result.get("live"):
        return False
    if not _is_payable(result):
        return False
    cons = constraints if isinstance(constraints, dict) else {}
    selected = result.get("selected_payment")
    if not selected_payment_is_complete(selected):
        selected = pick_selected_payment(result, objective, cons)
    if not selected_payment_is_complete(selected):
        return False
    return selected_payment_matches_networks(selected, cons)


def _options_for_constraints(result, cons) -> list[dict]:
    """Payment options that survive network + price bounds. Fail closed per option."""
    opts = payment_options(result)
    rails = cons.get("rails") if isinstance(cons, dict) else None
    if isinstance(rails, frozenset):
        opts = [o for o in opts if o.get("rail") in rails]
    max_amt = cons.get("max_amount_atomic") if isinstance(cons, dict) else None
    if max_amt is not None:
        kept = []
        for opt in opts:
            # Atomic bound only when the asset is known so units are meaningful.
            if opt.get("decimals") is None or opt.get("amount_atomic") is None:
                continue
            if int(opt["amount_atomic"]) > int(max_amt):
                continue
            kept.append(opt)
        opts = kept
    max_usd = cons.get("max_price_usd") if isinstance(cons, dict) else None
    if max_usd is not None:
        opts = [
            o
            for o in opts
            if o.get("normalized_usd") is not None and float(o["normalized_usd"]) <= float(max_usd)
        ]
    return opts


def latency_ms(result) -> int | None:
    """This-request probe RTT. Not historical service latency."""
    if not isinstance(result, dict):
        return None
    n = _as_int(result.get("latency_ms"))
    if n is not None:
        return n
    health = result.get("health") if isinstance(result.get("health"), dict) else {}
    return _as_int(health.get("latency_ms"))


def service_latency_ms(result) -> int | None:
    """Historical p50 from 402signal_observed. Unknown if never measured."""
    if not isinstance(result, dict):
        return None
    hist = result.get("history") if isinstance(result.get("history"), dict) else {}
    return _as_int(hist.get("p50_latency_ms"))


def observation_count(result) -> int | None:
    """n_7d observations. None if history is missing so a bound can fail closed."""
    if not isinstance(result, dict):
        return None
    hist = result.get("history") if isinstance(result.get("history"), dict) else None
    if not isinstance(hist, dict):
        return None
    n = _as_int(hist.get("n_7d"))
    if n is None:
        return 0
    return n if n >= 0 else 0


def _reliability_window(result) -> tuple[int, float | None]:
    """Return (n, rate) for the preferred history window. Rate is None when unknown."""
    if not isinstance(result, dict):
        return 0, None
    hist = result.get("history") if isinstance(result.get("history"), dict) else {}
    n_7d = _as_int(hist.get("n_7d")) or 0
    n_24h = _as_int(hist.get("n_24h")) or 0
    if n_7d >= WEAK_MIN_N:
        return n_7d, _as_float(hist.get("success_7d"))
    if n_24h >= WEAK_MIN_N:
        return n_24h, _as_float(hist.get("success_24h"))
    return 0, None


def reliability(result) -> float | None:
    """History rate when n >= 3. None is unknown, never 0.0. Ranking treats n<10 as weak."""
    n, rate = _reliability_window(result)
    if n >= WEAK_MIN_N:
        return rate
    return None


def mature_reliability(result) -> float | None:
    """Rate only when n >= 10. None is unknown, never 0.0."""
    n, rate = _reliability_window(result)
    if n >= MATURE_N:
        return rate
    return None


def weak_reliability(result) -> float | None:
    """Rate only when 3 <= n < 10. Last-rank tie-break. None is unknown, never 0.0."""
    n, rate = _reliability_window(result)
    if WEAK_MIN_N <= n < MATURE_N:
        return rate
    return None


def _payto(result) -> str | None:
    return _text(result.get("payTo")) if isinstance(result, dict) else None


def _is_payable(result) -> bool:
    """True iff at least one complete CURRENT observed payment option exists. Fail closed."""
    if not isinstance(result, dict) or not result.get("live"):
        return False
    env = result.get("envelope") if isinstance(result.get("envelope"), dict) else {}
    for opt in payment_options(result):
        if payment.is_complete_payment_option(opt, env):
            return True
    return False


def pick_selected_payment(result, objective=None, constraints=None) -> dict | None:
    """Exact CURRENT OBSERVED option that made this route win. Never catalog-only."""
    if not isinstance(result, dict) or not result.get("live"):
        return None
    env = result.get("envelope") if isinstance(result.get("envelope"), dict) else {}
    cons = constraints if isinstance(constraints, dict) else {}
    opts = _options_for_constraints(result, cons)
    complete = [o for o in opts if payment.is_complete_payment_option(o, env)]
    if not complete:
        return None
    obj = parse_objective(objective)
    usd = [o for o in complete if o.get("normalized_usd") is not None]
    if obj == "cheapest" or usd:
        if usd:
            complete = sorted(usd, key=lambda o: float(o["normalized_usd"]))
        else:
            keys = {payment.asset_identity(o) for o in complete}
            keys.discard(None)
            if len(keys) == 1:
                complete = sorted(
                    [o for o in complete if o.get("amount_atomic") is not None],
                    key=lambda o: int(o["amount_atomic"]),
                )
    if obj == "lowest_total_cost":
        priced = []
        for o in complete:
            eco = economics.for_option(o, result)
            cost = (eco.get("total_cost_usd") or {}).get("value")
            if cost is None:
                continue
            priced.append((float(cost), o))
        if not priced:
            return None
        priced.sort(key=lambda x: x[0])
        complete = [priced[0][1]]
    elif obj == "fastest_settlement":
        timed = []
        for o in complete:
            eco = economics.for_option(o, result)
            lat = (eco.get("settlement_or_finality_ms") or {}).get("value")
            if lat is None:
                continue
            timed.append((int(lat), o))
        if not timed:
            return None
        timed.sort(key=lambda x: x[0])
        complete = [timed[0][1]]
    picked = complete[0]
    fields = payment.selected_payment_fields(picked)
    if fields:
        fields["economics"] = economics.for_option(picked, result)
    return fields


def passes_constraints(result, constraints) -> bool:
    """Fail closed. Missing metrics fail a bound. payTo_changed is flagged, not rejected."""
    if not isinstance(result, dict):
        return False
    if not result.get("live"):
        return False
    if not _is_payable(result):
        return False
    cons = constraints if isinstance(constraints, dict) else {}
    if cons.get("unmeasured"):
        return False
    rails = cons.get("rails")
    if isinstance(rails, frozenset):
        # Catalog result.rail is not a matching observed option. Fail closed.
        if not _complete_options_for_constraints(result, cons):
            return False
    elif cons.get("max_amount_atomic") is not None or cons.get("max_price_usd") is not None:
        # Unknown or cross-asset atomic cannot apply the bound. Drop the candidate.
        if not _options_for_constraints(result, cons):
            return False
    max_probe = cons.get("max_probe_latency_ms")
    if max_probe is None:
        max_probe = cons.get("max_latency_ms")
    if max_probe is not None:
        lat = latency_ms(result)
        if lat is None or lat > max_probe:
            return False
    max_service = cons.get("max_service_latency_ms")
    if max_service is not None:
        svc = service_latency_ms(result)
        if svc is None or svc > max_service:
            return False
    min_obs = cons.get("min_observations")
    if min_obs is not None:
        n = observation_count(result)
        if n is None or n < min_obs:
            return False
    min_success = cons.get("min_observed_success")
    if min_success is not None:
        rate = reputation.observed_success_of(result)
        if rate is None or float(rate) < float(min_success):
            return False
    min_rep = cons.get("min_reputation_score")
    if min_rep is not None:
        score = reputation.score_of(result)
        if score is None or float(score) < float(min_rep):
            return False
    min_conf = cons.get("min_reputation_confidence")
    if min_conf is not None:
        conf = reputation.confidence_of(result)
        if conf is None or float(conf) < float(min_conf):
            return False
    max_settle = cons.get("max_settlement_latency_ms")
    if max_settle is not None:
        settle = economics.settlement_or_finality_ms(result)
        if settle is None or int(settle) > int(max_settle):
            return False
    max_total = cons.get("max_total_cost_usd")
    if max_total is not None:
        cost = economics.total_cost_usd(result)
        if cost is None or float(cost) > float(max_total):
            return False
    if cons.get("require_invocable") and not result.get("invocable"):
        return False
    return True


def selected_payment_passes_constraints(result, selected, constraints) -> bool:
    """Apply every option-specific constraint to the exact selected payment.

    Candidate-level checks may accept a result when any observed option fits.
    The final economic gate must never substitute that option for the one the
    route actually selected.
    """
    if not isinstance(result, dict) or not selected_payment_is_complete(selected):
        return False
    cons = constraints if isinstance(constraints, dict) else {}
    if cons.get("unmeasured"):
        return False
    keys = (
        "rail",
        "network",
        "asset",
        "amount_atomic",
        "display_amount",
        "normalized_usd",
        "payTo",
        "facilitator",
    )
    eligible = _complete_options_for_constraints(result, cons)
    if not any(
        all(selected.get(key) == payment.selected_payment_fields(opt).get(key) for key in keys)
        for opt in eligible
        if payment.selected_payment_fields(opt) is not None
    ):
        return False
    max_total = cons.get("max_total_cost_usd")
    if max_total is not None:
        total = economics.total_cost_usd(result, selected)
        if total is None or float(total) > float(max_total):
            return False
    max_settle = cons.get("max_settlement_latency_ms")
    if max_settle is not None:
        latency = economics.settlement_or_finality_ms(result, selected)
        if latency is None or int(latency) > int(max_settle):
            return False
    common = dict(cons)
    for key in (
        "rails",
        "max_amount_atomic",
        "max_price_usd",
        "max_total_cost_usd",
        "max_settlement_latency_ms",
    ):
        common[key] = None
    return passes_constraints(result, common)


def _readiness_tier(result) -> int:
    """invocable > payable > live. Not a rail rank. Not catalog traction."""
    if not isinstance(result, dict):
        return -1
    if result.get("invocable"):
        return 2
    if _is_payable(result):
        return 1
    if result.get("live"):
        return 0
    return -1


def _best_usd(result, cons=None) -> float | None:
    cons = cons if isinstance(cons, dict) else {}
    opts = _options_for_constraints(result, cons)
    if not opts:
        # Do not fall back to another rail when networks locked this candidate.
        if isinstance(cons.get("rails"), frozenset):
            return None
        opts = payment_options(result)
    vals = [o.get("normalized_usd") for o in opts if o.get("normalized_usd") is not None]
    if not vals:
        return None
    return min(float(v) for v in vals)


def _best_atomic_for_asset(result, asset_key: str, cons=None) -> int | None:
    cons = cons if isinstance(cons, dict) else {}
    opts = _options_for_constraints(result, cons)
    if not opts:
        if isinstance(cons.get("rails"), frozenset):
            return None
        opts = payment_options(result)
    vals = []
    for opt in opts:
        if payment.asset_identity(opt) != asset_key:
            continue
        n = _as_int(opt.get("amount_atomic"))
        if n is not None:
            vals.append(n)
    if not vals:
        return None
    return min(vals)


def _cmp_amount_asc(a, b) -> int:
    """Compare prices only when both sides are USD-known or the same known asset."""
    ua, ub = _best_usd(a), _best_usd(b)
    if ua is not None and ub is not None:
        if ua < ub:
            return -1
        if ua > ub:
            return 1
        return 0
    keys_a = {payment.asset_identity(o) for o in payment_options(a)}
    keys_b = {payment.asset_identity(o) for o in payment_options(b)}
    keys_a.discard(None)
    keys_b.discard(None)
    shared = keys_a & keys_b
    if len(shared) == 1:
        key = next(iter(shared))
        aa, ab = _best_atomic_for_asset(a, key), _best_atomic_for_asset(b, key)
        if aa is not None and ab is not None:
            if aa < ab:
                return -1
            if aa > ab:
                return 1
            return 0
    # Incomparable: do not treat unknown atomic as cheaper/dearer.
    return 0


def _cheapest_comparable_subset(results, cons) -> list[dict]:
    """Keep only results that can be cheapest-ranked. Fail closed on mixed unknowns.

    Known-USDC options compare via normalized_usd. Unknown tokens are dropped
    when any USD-known option exists. Two different unknown tokens → empty.
    Same known asset → amount_atomic is OK.
    """
    priced: list[tuple] = []
    for result in results:
        opts = _options_for_constraints(result, cons)
        if not opts:
            if isinstance(cons.get("rails"), frozenset):
                continue
            opts = payment_options(result)
        usd = [o for o in opts if o.get("normalized_usd") is not None]
        if usd:
            priced.append((result, "usd", min(float(o["normalized_usd"]) for o in usd), None))
            continue
        keys = {payment.asset_identity(o) for o in opts}
        keys.discard(None)
        if len(keys) == 1:
            key = next(iter(keys))
            atomics = [_as_int(o.get("amount_atomic")) for o in opts if payment.asset_identity(o) == key]
            atomics = [n for n in atomics if n is not None]
            if atomics:
                priced.append((result, "atomic", min(atomics), key))
    if not priced:
        return []
    if any(kind == "usd" for _r, kind, _v, _k in priced):
        return [r for r, kind, _v, _k in priced if kind == "usd"]
    assets = {k for _r, kind, _v, k in priced if kind == "atomic"}
    if len(assets) == 1:
        return [r for r, kind, _v, _k in priced if kind == "atomic"]
    return []


def _cmp_latency_asc(a, b, unknown_last: bool) -> int:
    la, lb = latency_ms(a), latency_ms(b)
    if la is not None and lb is not None:
        if la < lb:
            return -1
        if la > lb:
            return 1
        return 0
    if unknown_last:
        if la is not None:
            return -1
        if lb is not None:
            return 1
    return 0


def _cmp_rate_desc(ra, rb) -> int:
    if ra is not None and rb is not None:
        if ra > rb:
            return -1
        if ra < rb:
            return 1
        return 0
    if ra is not None:
        return -1
    if rb is not None:
        return 1
    return 0


def _cmp_mature_reliability_desc(a, b) -> int:
    return _cmp_rate_desc(mature_reliability(a), mature_reliability(b))


def _cmp_weak_reliability_desc(a, b) -> int:
    return _cmp_rate_desc(weak_reliability(a), weak_reliability(b))


def _cmp_cheapest(a, b) -> int:
    c = _cmp_amount_asc(a, b)
    if c:
        return c
    # Tie: lower latency only if both known; else keep first.
    return _cmp_latency_asc(a, b, unknown_last=False)


def _cmp_fastest(a, b) -> int:
    c = _cmp_latency_asc(a, b, unknown_last=True)
    if c:
        return c
    return _cmp_amount_asc(a, b)


def _cmp_most_reliable(a, b) -> int:
    c = _cmp_mature_reliability_desc(a, b)
    if c:
        return c
    c = _cmp_weak_reliability_desc(a, b)
    if c:
        return c
    c = _cmp_latency_asc(a, b, unknown_last=True)
    if c:
        return c
    return _cmp_amount_asc(a, b)


def _total_cost(result) -> float | None:
    return economics.total_cost_usd(result)


def _settlement_ms(result) -> int | None:
    return economics.settlement_or_finality_ms(result)


def _cmp_lowest_total_cost(a, b) -> int:
    ca, cb = _total_cost(a), _total_cost(b)
    if ca is not None and cb is not None:
        if ca < cb:
            return -1
        if ca > cb:
            return 1
        return _cmp_latency_asc(a, b, unknown_last=False)
    if ca is not None:
        return -1
    if cb is not None:
        return 1
    return 0


def _cmp_fastest_settlement(a, b) -> int:
    sa, sb = _settlement_ms(a), _settlement_ms(b)
    if sa is not None and sb is not None:
        if sa < sb:
            return -1
        if sa > sb:
            return 1
        return _cmp_amount_asc(a, b)
    if sa is not None:
        return -1
    if sb is not None:
        return 1
    return 0


def _cmp_best(a, b) -> int:
    ta, tb = _readiness_tier(a), _readiness_tier(b)
    if ta != tb:
        return -1 if ta > tb else 1
    c = _cmp_mature_reliability_desc(a, b)
    if c:
        return c
    la, lb = latency_ms(a), latency_ms(b)
    if la is not None and lb is not None and la != lb:
        return -1 if la < lb else 1
    c = _cmp_amount_asc(a, b)
    if c:
        return c
    return _cmp_weak_reliability_desc(a, b)


_CMP = {
    "cheapest": _cmp_cheapest,
    "fastest": _cmp_fastest,
    "most_reliable": _cmp_most_reliable,
    "lowest_total_cost": _cmp_lowest_total_cost,
    "fastest_settlement": _cmp_fastest_settlement,
    "best": _cmp_best,
}


def _payto_selectable(result: dict, constraints: dict | None = None) -> bool:
    """First unexpected last_payTo rotation is not selectable unless accept_payTo_change.

    Catalog claimed vs observed mismatch (payTo_changed) stays selectable.
    """
    if not isinstance(result, dict):
        return False
    if not result.get("payTo_pending"):
        return True
    cons = constraints if isinstance(constraints, dict) else {}
    return bool(cons.get("accept_payTo_change"))


def enough_evidence(results: list[dict], objective: str, constraints: dict | None = None) -> bool:
    """True when a completed tranche has a selectable winner; do not start another.

    Call only after already-running candidates have finished. Does not cancel
    in-flight work. First last_payTo rotation (payTo_pending) is not enough
    evidence unless accept_payTo_change. best keeps looking when every
    remaining viable hit is payTo_changed (claimed vs observed). Fail-closed:
    no viable → False.
    """
    if not isinstance(results, list) or not results:
        return False
    obj = parse_objective(objective)
    cons = constraints if isinstance(constraints, dict) else {}
    viable = [
        r
        for r in results
        if isinstance(r, dict) and passes_constraints(r, cons) and _payto_selectable(r, cons)
    ]
    if not viable:
        return False
    stable = [r for r in viable if not r.get("payTo_changed")]
    if obj == "best":
        return bool(stable)
    if obj == "cheapest":
        pool = stable or viable
        return bool(_cheapest_comparable_subset(pool, cons))
    if obj == "lowest_total_cost":
        pool = stable or viable
        return any(_total_cost(r) is not None for r in pool)
    if obj == "fastest_settlement":
        pool = stable or viable
        return any(_settlement_ms(r) is not None for r in pool)
    return True


def pick_winner(results: list[dict], objective: str, constraints: dict | None = None) -> dict | None:
    """Filter fail-closed, then pick. None means caller keeps the current miss.

    First last_payTo rotation (payTo_pending) is dropped unless
    accept_payTo_change. Claimed vs observed (payTo_changed) stays
    pickable so catalog mismatch does not brick a live dest.

    A winner without a complete CURRENT observed selected_payment is not a
    winner. networks is a hard lock on that observed option, never on a
    catalog rail tag.
    """
    if not isinstance(results, list) or not results:
        return None
    obj = parse_objective(objective)
    cons = constraints if isinstance(constraints, dict) else {}
    remaining = [
        r
        for r in results
        if isinstance(r, dict) and passes_constraints(r, cons) and _payto_selectable(r, cons)
    ]
    if not remaining:
        return None
    if obj == "cheapest":
        remaining = _cheapest_comparable_subset(remaining, cons)
        if not remaining:
            return None
    if obj == "lowest_total_cost":
        remaining = [r for r in remaining if _total_cost(r) is not None]
        if not remaining:
            return None
    if obj == "fastest_settlement":
        remaining = [r for r in remaining if _settlement_ms(r) is not None]
        if not remaining:
            return None
    cmp_fn = _CMP.get(obj, _cmp_best)
    # Stable: original remaining order is the last tie-break (first wins).
    ranked = sorted(remaining, key=cmp_to_key(cmp_fn))
    for candidate in ranked:
        if pick_selected_payment(candidate, obj, cons) is not None:
            return candidate
    return None


def _readiness_label(result) -> str | None:
    raw = result.get("readiness")
    if raw:
        return str(raw)
    if result.get("invocable"):
        return "invocable"
    if _is_payable(result):
        return "payable"
    if result.get("live"):
        return "discovered"
    return result.get("readiness")


def _compared_7d(result) -> tuple[int, float | None]:
    """Factual 7d window for compared[]. Rate is None when n_7d < 3."""
    if not isinstance(result, dict):
        return 0, None
    hist = result.get("history") if isinstance(result.get("history"), dict) else {}
    n_7d = _as_int(hist.get("n_7d")) or 0
    if n_7d < 0:
        n_7d = 0
    if n_7d < WEAK_MIN_N:
        return n_7d, None
    return n_7d, _as_float(hist.get("success_7d"))


def _is_winner_result(result, winner) -> bool:
    if not isinstance(result, dict) or not isinstance(winner, dict):
        return False
    if result is winner:
        return True
    url = result.get("url")
    return bool(url) and url == winner.get("url")


def _compared_row(result, selected, pay) -> dict:
    n_7d, success_7d = _compared_7d(result)
    row = {
        "url": result.get("url"),
        "rail": (pay or {}).get("rail") or result.get("rail"),
        "amount_atomic": (pay or {}).get("amount_atomic")
        if pay and pay.get("amount_atomic") is not None
        else amount_atomic(result),
        "latency_ms": latency_ms(result),
        "success_7d": success_7d,
        "n_7d": n_7d,
        "readiness": _readiness_label(result),
        "live": bool(result.get("live")),
        "invocable": bool(result.get("invocable")),
        "selected": bool(selected),
    }
    if selected and pay:
        row["selected_payment"] = pay
    rep = reputation.public_row(result)
    if rep:
        row["reputation"] = rep
    eco = None
    if pay and pay.get("economics"):
        eco = pay.get("economics")
    else:
        eco = economics.for_result(result, pay)
    if eco:
        row["economics"] = eco
    return row


def comparison(results, winner, objective=None, constraints=None) -> list[dict]:
    """Slim rows for a later `compared` field. Cap COMPARED_CAP.

    n<3 → success_7d is None. amount_atomic / rail / selected_payment on the
    winner row are the same CURRENT OBSERVED option stored on selected_payment.
    The winner always occupies a slot even when the list is capped.
    """
    rows: list[dict] = []
    if not isinstance(results, list):
        results = []
    winner_pay = None
    if isinstance(winner, dict):
        winner_pay = winner.get("selected_payment")
        if not isinstance(winner_pay, dict):
            winner_pay = pick_selected_payment(winner, objective, constraints)
    winner_row = None
    for result in results:
        if not isinstance(result, dict):
            continue
        selected = _is_winner_result(result, winner)
        pay = winner_pay if selected else pick_selected_payment(result, objective, constraints)
        row = _compared_row(result, selected, pay)
        if selected:
            winner_row = row
        rows.append(row)
    if winner_row is None and isinstance(winner, dict):
        winner_row = _compared_row(winner, True, winner_pay)
    if winner_row is None:
        return rows[:COMPARED_CAP]
    if any(r.get("selected") for r in rows[:COMPARED_CAP]) and len(rows) <= COMPARED_CAP:
        return rows
    if any(r.get("selected") for r in rows[:COMPARED_CAP]):
        return rows[:COMPARED_CAP]
    others = [r for r in rows if not r.get("selected")]
    return others[: max(0, COMPARED_CAP - 1)] + [winner_row]


def unmet_constraint_names(result, constraints) -> list[str]:
    """Named bounds this live candidate failed. Empty if it would pass.

    Tiny-price and high-min-observation misses stay distinct names.
    """
    if not isinstance(result, dict) or not isinstance(constraints, dict):
        return []
    if not result.get("live"):
        return []
    unmet: list[str] = []
    rails = constraints.get("rails")
    if isinstance(rails, frozenset) and not _complete_options_for_constraints(result, constraints):
        unmet.append("networks")
    if constraints.get("max_amount_atomic") is not None or constraints.get("max_price_usd") is not None:
        if not _options_for_constraints(result, constraints):
            if constraints.get("max_price_usd") is not None:
                unmet.append("max_price_usd")
            if constraints.get("max_amount_atomic") is not None:
                unmet.append("max_amount_atomic")
    max_probe = constraints.get("max_probe_latency_ms")
    if max_probe is None:
        max_probe = constraints.get("max_latency_ms")
    if max_probe is not None:
        lat = latency_ms(result)
        if lat is None or lat > max_probe:
            unmet.append("max_probe_latency_ms")
    if constraints.get("max_service_latency_ms") is not None:
        svc = service_latency_ms(result)
        if svc is None or svc > constraints["max_service_latency_ms"]:
            unmet.append("max_service_latency_ms")
    if constraints.get("min_observations") is not None:
        n = observation_count(result)
        if n is None or n < constraints["min_observations"]:
            unmet.append("min_observations")
    if constraints.get("min_observed_success") is not None:
        rate = reputation.observed_success_of(result)
        if rate is None or float(rate) < float(constraints["min_observed_success"]):
            unmet.append("min_observed_success")
    if constraints.get("min_reputation_score") is not None:
        score = reputation.score_of(result)
        if score is None or float(score) < float(constraints["min_reputation_score"]):
            unmet.append("min_reputation_score")
    if constraints.get("min_reputation_confidence") is not None:
        conf = reputation.confidence_of(result)
        if conf is None or float(conf) < float(constraints["min_reputation_confidence"]):
            unmet.append("min_reputation_confidence")
    if constraints.get("max_settlement_latency_ms") is not None:
        settle = economics.settlement_or_finality_ms(result)
        if settle is None or int(settle) > int(constraints["max_settlement_latency_ms"]):
            unmet.append("max_settlement_latency_ms")
    if constraints.get("max_total_cost_usd") is not None:
        cost = economics.total_cost_usd(result)
        if cost is None or float(cost) > float(constraints["max_total_cost_usd"]):
            unmet.append("max_total_cost_usd")
    if constraints.get("require_invocable") and not result.get("invocable"):
        unmet.append("require_invocable")
    return unmet


def collect_unmet_constraints(results, constraints) -> list[str]:
    """Unique unmet bound names across evaluated live candidates, stable order."""
    names: list[str] = []
    seen: set[str] = set()
    if not isinstance(results, list):
        return names
    for result in results:
        for name in unmet_constraint_names(result, constraints):
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
    return names
