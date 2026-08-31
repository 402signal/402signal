"""Constraint-aware best-of-N selection. Rail-neutral PRE-PAYMENT SIGNAL. Fail closed. Never pay."""

from __future__ import annotations

from functools import cmp_to_key

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
    return {
        "max_amount_atomic": _nonneg_int(src.get("max_amount_atomic")),
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
    return _amount_from_accepts(env.get("accepts"))


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
    return bool(isinstance(result, dict) and result.get("live") and _payto(result))


def passes_constraints(result, constraints) -> bool:
    """Fail closed. Missing metrics fail a bound. payTo_changed is flagged, not rejected."""
    if not isinstance(result, dict):
        return False
    if not result.get("live"):
        return False
    if not _payto(result):
        return False
    cons = constraints if isinstance(constraints, dict) else {}
    max_amt = cons.get("max_amount_atomic")
    if max_amt is not None:
        amt = amount_atomic(result)
        if amt is None or amt > max_amt:
            return False
    max_lat = cons.get("max_latency_ms")
    if max_lat is not None:
        lat = latency_ms(result)
        if lat is None or lat > max_lat:
            return False
    if cons.get("require_invocable") and not result.get("invocable"):
        return False
    rails = cons.get("rails")
    if isinstance(rails, frozenset) and result.get("rail") not in rails:
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


def _cmp_amount_asc(a, b) -> int:
    aa, ab = amount_atomic(a), amount_atomic(b)
    if aa is not None and ab is not None:
        if aa < ab:
            return -1
        if aa > ab:
            return 1
        return 0
    if aa is not None:
        return -1
    if ab is not None:
        return 1
    return 0


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
    aa, ab = amount_atomic(a), amount_atomic(b)
    if aa is not None and ab is not None:
        if aa < ab:
            return -1
        if aa > ab:
            return 1
    return 0


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
    aa, ab = amount_atomic(a), amount_atomic(b)
    if aa is not None and ab is not None:
        if aa < ab:
            return -1
        if aa > ab:
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
    aa, ab = amount_atomic(a), amount_atomic(b)
    if aa is not None and ab is not None and aa != ab:
        return -1 if aa < ab else 1
    return _cmp_weak_reliability_desc(a, b)


_CMP = {
    "cheapest": _cmp_cheapest,
    "fastest": _cmp_fastest,
    "most_reliable": _cmp_most_reliable,
    "best": _cmp_best,
}


def enough_evidence(results: list[dict], objective: str, constraints: dict | None = None) -> bool:
    """True when more probes would not change a selectable winner for this objective.

    best: one stable (not payTo_changed) live+payTo that passes constraints.
    cheapest/fastest/most_reliable: at least two viable hits so comparison is real.
    payTo_changed-only windows keep looking. Fail-closed: no viable → False.
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
    return len(stable or viable) >= 2


def pick_winner(results: list[dict], objective: str, constraints: dict | None = None) -> dict | None:
    """Filter fail-closed, then pick. None means caller keeps the current miss."""
    if not isinstance(results, list) or not results:
        return None
    obj = parse_objective(objective)
    cons = constraints if isinstance(constraints, dict) else {}
    remaining = [r for r in results if isinstance(r, dict) and passes_constraints(r, cons)]
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


def comparison(results, winner) -> list[dict]:
    """Slim rows for a later `compared` field. Cap 5. Unknown rates stay None."""
    rows: list[dict] = []
    if not isinstance(results, list):
        return rows
    for result in results:
        if not isinstance(result, dict):
            continue
        rows.append(
            {
                "url": result.get("url"),
                "rail": result.get("rail"),
                "amount_atomic": amount_atomic(result),
                "latency_ms": latency_ms(result),
                "reliability": reliability(result),
                "readiness": _readiness_label(result),
                "live": bool(result.get("live")),
                "invocable": bool(result.get("invocable")),
                "selected": result is winner,
            }
        )
        if len(rows) >= 5:
            break
    return rows
