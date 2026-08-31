"""Constraint-aware best-of-N selection. Rail-neutral PRE-PAYMENT SIGNAL. Fail closed. Never pay."""

from __future__ import annotations

from functools import cmp_to_key

from live402 import payment

OBJECTIVES = ("best", "cheapest", "fastest", "most_reliable")
RAILS = frozenset(("base", "solana", "algorand"))
WEAK_MIN_N = 3
MATURE_N = 10


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
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = list(raw)
    else:
        return None
    rails = []
    for item in items:
        name = _text(item)
        if not name:
            continue
        name = name.lower()
        if name in RAILS:
            rails.append(name)
    if not rails:
        return None
    return frozenset(rails)


def parse_constraints(body: dict) -> dict:
    """Normalize caller constraints. Invalid numeric bounds → unconstrained (None)."""
    src = body if isinstance(body, dict) else {}
    networks = src.get("networks")
    if networks is None:
        networks = src.get("rails")
    max_usd = _as_float(src.get("max_price_usd"))
    if max_usd is not None and max_usd < 0:
        max_usd = None
    return {
        "max_amount_atomic": _nonneg_int(src.get("max_amount_atomic")),
        "max_price_usd": max_usd,
        "max_latency_ms": _nonneg_int(src.get("max_latency_ms")),
        "require_invocable": _truthy(src.get("require_invocable"), False),
        "rails": _parse_rails(networks),
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
    rails: set = set()
    if isinstance(result, dict) and result.get("rail") in RAILS:
        rails.add(result.get("rail"))
    for opt in payment_options(result):
        if opt.get("rail") in RAILS:
            rails.add(opt["rail"])
    return rails


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
    if not isinstance(result, dict):
        return None
    n = _as_int(result.get("latency_ms"))
    if n is not None:
        return n
    health = result.get("health") if isinstance(result.get("health"), dict) else {}
    return _as_int(health.get("latency_ms"))


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
    picked = complete[0]
    return payment.selected_payment_fields(picked)


def passes_constraints(result, constraints) -> bool:
    """Fail closed. Missing metrics fail a bound. payTo_changed is flagged, not rejected."""
    if not isinstance(result, dict):
        return False
    if not result.get("live"):
        return False
    if not _is_payable(result):
        return False
    cons = constraints if isinstance(constraints, dict) else {}
    rails = cons.get("rails")
    if isinstance(rails, frozenset):
        if _result_rails(result).isdisjoint(rails):
            return False
    if cons.get("max_amount_atomic") is not None or cons.get("max_price_usd") is not None:
        # Unknown or cross-asset atomic cannot apply the bound — drop the candidate.
        if not _options_for_constraints(result, cons):
            return False
    max_lat = cons.get("max_latency_ms")
    if max_lat is not None:
        lat = latency_ms(result)
        if lat is None or lat > max_lat:
            return False
    if cons.get("require_invocable") and not result.get("invocable"):
        return False
    return True


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
    opts = _options_for_constraints(result, cons or {})
    if not opts:
        opts = payment_options(result)
    vals = [o.get("normalized_usd") for o in opts if o.get("normalized_usd") is not None]
    if not vals:
        return None
    return min(float(v) for v in vals)


def _best_atomic_for_asset(result, asset_key: str, cons=None) -> int | None:
    opts = _options_for_constraints(result, cons or {})
    if not opts:
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
    "best": _cmp_best,
}


def enough_evidence(results: list[dict], objective: str, constraints: dict | None = None) -> bool:
    """True when a completed tranche has a selectable winner; do not start another.

    Call only after already-running candidates have finished. Does not cancel
    in-flight work. best keeps looking when every viable hit is payTo_changed.
    Fail-closed: no viable → False.
    """
    if not isinstance(results, list) or not results:
        return False
    obj = parse_objective(objective)
    cons = constraints if isinstance(constraints, dict) else {}
    viable = [r for r in results if isinstance(r, dict) and passes_constraints(r, cons)]
    if not viable:
        return False
    stable = [r for r in viable if not r.get("payTo_changed")]
    if obj == "best":
        return bool(stable)
    if obj == "cheapest":
        pool = stable or viable
        return bool(_cheapest_comparable_subset(pool, cons))
    return bool(stable or viable)


def pick_winner(results: list[dict], objective: str, constraints: dict | None = None) -> dict | None:
    """Filter fail-closed, then pick. None means caller keeps the current miss."""
    if not isinstance(results, list) or not results:
        return None
    obj = parse_objective(objective)
    cons = constraints if isinstance(constraints, dict) else {}
    remaining = [r for r in results if isinstance(r, dict) and passes_constraints(r, cons)]
    if not remaining:
        return None
    if obj == "cheapest":
        remaining = _cheapest_comparable_subset(remaining, cons)
        if not remaining:
            return None
    cmp_fn = _CMP.get(obj, _cmp_best)
    # Stable: original remaining order is the last tie-break (first wins).
    ranked = sorted(remaining, key=cmp_to_key(cmp_fn))
    return ranked[0]


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


def comparison(results, winner, objective=None, constraints=None) -> list[dict]:
    """Slim rows for a later `compared` field. Cap 5. n<3 → success_7d is None.

    amount_atomic / rail / selected_payment on the winner row are the same
    CURRENT OBSERVED option stored on selected_payment.
    """
    rows: list[dict] = []
    if not isinstance(results, list):
        return rows
    winner_pay = None
    if isinstance(winner, dict):
        winner_pay = winner.get("selected_payment")
        if not isinstance(winner_pay, dict):
            winner_pay = pick_selected_payment(winner, objective, constraints)
    for result in results:
        if not isinstance(result, dict):
            continue
        n_7d, success_7d = _compared_7d(result)
        selected = result is winner
        pay = winner_pay if selected else pick_selected_payment(result, objective, constraints)
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
            "selected": selected,
        }
        if selected and pay:
            row["selected_payment"] = pay
        rows.append(row)
        if len(rows) >= 5:
            break
    return rows
