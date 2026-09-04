"""Compile a short natural-language policy into structured constraints.

The selection engine uses structured values only. Safety-critical bounds
that cannot be read unambiguously are returned in unresolved_constraints
and are never guessed.
"""

from __future__ import annotations

import re

from live402 import select

_PRICE = re.compile(
    r"(?:under|below|less\s+than|at\s+most|max(?:imum)?|<=|<)\s*\$?\s*(\d+(?:\.\d+)?)\s*(usd|dollars?|cents?)?",
    re.I,
)
_PRICE_BARE = re.compile(r"\$\s*(\d+(?:\.\d+)?)", re.I)
_CENTS = re.compile(
    r"(?:under|below|less\s+than|at\s+most|max(?:imum)?|<=|<)\s*(\d+)\s*cents?",
    re.I,
)
_MS = re.compile(
    r"(?:under|below|less\s+than|at\s+most|max(?:imum)?|<=|<|within)?\s*(\d+)\s*m(?:illi)?s(?:ec(?:ond)?s?)?",
    re.I,
)
_OBS = re.compile(
    r"(?:at\s+least|min(?:imum)?|>=|>)\s*(\d+)\s*(?:observations?|probes?|samples?)",
    re.I,
)
_NETWORK = re.compile(
    r"\b(?:on|via|using|only|just|network(?:s)?(?:\s*[:=])?)\s+(base|solana|algorand)\b",
    re.I,
)
_NETWORK_ONLY = re.compile(r"\b(base|solana|algorand)\s+only\b", re.I)
_INVOCABLE = re.compile(r"\b(?:require[sd]?|must\s+be|need[s]?)\s+invocable\b|\binvocable\s+only\b", re.I)

# Vague adjectives. Never invent a reputation score threshold from these.
_UNRESOLVED_HINTS = (
    (re.compile(r"\breputat", re.I), "min_reputation_score"),
    (re.compile(r"\btrust\s*score\b", re.I), "min_reputation_score"),
    (re.compile(r"\bsettlement\b", re.I), "max_settlement_latency_ms"),
    (re.compile(r"\bsuccess(?:\s+rate)?\b", re.I), "min_observed_success"),
    (re.compile(r"\b(?:reliable|reliability)\b", re.I), "min_observed_success"),
    (re.compile(r"\btotal\s+cost\b", re.I), "max_total_cost_usd"),
)
_ESTABLISHED = re.compile(
    r"\b(?:established\s+usage|strong\s+observed\s+evidence|well[-\s]?established)\b",
    re.I,
)
_TOTAL_COST = re.compile(
    r"total\s+cost.{0,24}(?:under|below|less\s+than|at\s+most|max(?:imum)?|<=|<)\s*\$?\s*(\d+(?:\.\d+)?)",
    re.I,
)
_TOTAL_COST_BARE = re.compile(
    r"(?:under|below|less\s+than|at\s+most|max(?:imum)?|<=|<)\s*\$?\s*(\d+(?:\.\d+)?).{0,16}total\s+cost",
    re.I,
)
_SETTLEMENT_MS = re.compile(
    r"settlement.{0,24}(?:under|below|less\s+than|at\s+most|max(?:imum)?|<=|<|within)?\s*(\d+)\s*m(?:illi)?s(?:ec(?:ond)?s?)?",
    re.I,
)
_SETTLEMENT_S = re.compile(
    r"settlement.{0,24}(?:under|below|less\s+than|at\s+most|max(?:imum)?|<=|<|within)?\s*(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?\b",
    re.I,
)

_VAGUE = (
    (re.compile(r"\bcheap(?:est)?\b", re.I), "max_price_usd"),
    (re.compile(r"\bfast(?:est)?\b", re.I), "max_probe_latency_ms"),
    (re.compile(r"\blow\s+latenc", re.I), "max_probe_latency_ms"),
)


def _f(val: str) -> float | None:
    try:
        n = float(val)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    return n


def compile_policy(text: str | None) -> dict:
    """Parse NL into interpreted_constraints + unresolved_constraints.

    Bare millisecond bounds map to max_probe_latency_ms (same as max_latency_ms).
    Service latency is only set when the text says service/historical/p50.
    Networks, invocable, and observation floors are set only when explicit.
    Settlement / total-cost language compiles only with a numeric bound.
    "established usage" / "strong observed evidence" compile to
    min_observations=10 (mature n). Vague "high reputation" stays unresolved.
    """
    raw = " ".join(str(text or "").split())
    interpreted: dict = {}
    unresolved: list[dict] = []
    seen_unresolved: set[str] = set()

    def note_unresolved(name: str, fragment: str, reason: str) -> None:
        if name in seen_unresolved:
            return
        seen_unresolved.add(name)
        unresolved.append({"name": name, "fragment": fragment, "reason": reason})

    if not raw:
        return {"interpreted_constraints": interpreted, "unresolved_constraints": unresolved}

    low = raw.lower()

    cents = _CENTS.search(raw)
    priced = False
    if cents:
        n = _f(cents.group(1))
        if n is not None:
            interpreted["max_price_usd"] = round(n / 100.0, 6)
            priced = True
    if not priced:
        hit = _PRICE.search(raw)
        if hit:
            n = _f(hit.group(1))
            unit = (hit.group(2) or "usd").lower()
            if n is not None:
                if unit.startswith("cent"):
                    n = n / 100.0
                interpreted["max_price_usd"] = n
                priced = True
    if not priced:
        bare = _PRICE_BARE.search(raw)
        if bare:
            n = _f(bare.group(1))
            if n is not None:
                interpreted["max_price_usd"] = n
                priced = True

    for m in _MS.finditer(raw):
        n = select._nonneg_int(m.group(1))
        if n is None:
            continue
        span = raw[max(0, m.start() - 24) : m.end() + 24].lower()
        if "service" in span or "historical" in span or "p50" in span:
            interpreted["max_service_latency_ms"] = n
        elif "probe" in span or "rtt" in span:
            interpreted["max_probe_latency_ms"] = n
            interpreted["max_latency_ms"] = n
        else:
            # Compat: a bare millisecond bound is probe RTT, same as max_latency_ms.
            interpreted["max_probe_latency_ms"] = n
            interpreted["max_latency_ms"] = n

    obs = _OBS.search(raw)
    if obs:
        n = select._nonneg_int(obs.group(1))
        if n is not None:
            interpreted["min_observations"] = n

    rails = []
    for rx in (_NETWORK, _NETWORK_ONLY):
        for m in rx.finditer(raw):
            name = m.group(1).lower()
            if name in select.RAILS and name not in rails:
                rails.append(name)
    if rails:
        interpreted["networks"] = rails

    if _INVOCABLE.search(raw):
        interpreted["require_invocable"] = True

    if _ESTABLISHED.search(raw):
        # Defensible floor: public reliability is hidden below n=10.
        interpreted["min_observations"] = 10

    settle_ms = _SETTLEMENT_MS.search(raw)
    if settle_ms:
        n = select._nonneg_int(settle_ms.group(1))
        if n is not None:
            interpreted["max_settlement_latency_ms"] = n
    else:
        settle_s = _SETTLEMENT_S.search(raw)
        if settle_s:
            n = _f(settle_s.group(1))
            if n is not None:
                interpreted["max_settlement_latency_ms"] = int(round(n * 1000))

    total = _TOTAL_COST.search(raw) or _TOTAL_COST_BARE.search(raw)
    if total:
        n = _f(total.group(1))
        if n is not None:
            interpreted["max_total_cost_usd"] = n

    for rx, name in _UNRESOLVED_HINTS:
        if name in interpreted:
            continue
        m = rx.search(raw)
        if m:
            reason = "no numeric bound; not guessed"
            if name == "min_reputation_score":
                reason = "vague adjective; never guessed as a score threshold"
            note_unresolved(name, m.group(0), reason)

    if not priced:
        for rx, name in _VAGUE:
            if name != "max_price_usd":
                continue
            m = rx.search(raw)
            if m:
                note_unresolved(name, m.group(0), "no numeric price bound")
    if "max_probe_latency_ms" not in interpreted and "max_service_latency_ms" not in interpreted:
        for rx, name in _VAGUE:
            if name != "max_probe_latency_ms":
                continue
            m = rx.search(raw)
            if m:
                note_unresolved(name, m.group(0), "no numeric latency bound")

    _ = low
    return {
        "interpreted_constraints": interpreted,
        "unresolved_constraints": unresolved,
    }


def merge_constraints(body: dict | None, compiled: dict | None = None) -> dict:
    """Structured body keys win. Engine input is structured only."""
    src = body if isinstance(body, dict) else {}
    compiled = compiled if isinstance(compiled, dict) else compile_policy(
        src.get("policy") if isinstance(src.get("policy"), str) and src.get("policy").strip()
        else src.get("need")
    )
    overlay = dict(compiled.get("interpreted_constraints") or {})
    for key in (
        "max_amount_atomic",
        "max_price_usd",
        "max_latency_ms",
        "max_probe_latency_ms",
        "max_service_latency_ms",
        "require_invocable",
        "networks",
        "rails",
        "min_observations",
        "min_observed_success",
        "min_reputation_score",
        "min_reputation_confidence",
        "max_settlement_latency_ms",
        "max_total_cost_usd",
        "accept_payTo_change",
        "require_transparency",
    ):
        if key in src:
            overlay[key] = src[key]
    cons = select.parse_constraints(overlay)
    return cons


def public_applied_constraints(cons: dict | None, body: dict | None = None) -> dict:
    """Echo constraints the engine actually used. Empty means unconstrained.

    Structured body keys and compiled NL that reached parse_constraints are
    included. prefer_network is echoed as a ranking preference only; it is
    never a networks lock.
    """
    src = body if isinstance(body, dict) else {}
    engine = cons if isinstance(cons, dict) else {}
    out: dict = {}
    rails = engine.get("rails")
    if isinstance(rails, frozenset):
        out["networks"] = sorted(rails)
    for key in (
        "max_amount_atomic",
        "max_price_usd",
        "max_latency_ms",
        "max_probe_latency_ms",
        "max_service_latency_ms",
        "min_observations",
        "min_observed_success",
        "min_reputation_score",
        "min_reputation_confidence",
        "max_total_cost_usd",
        "max_settlement_latency_ms",
    ):
        val = engine.get(key)
        if val is not None:
            out[key] = val
    if engine.get("require_invocable"):
        out["require_invocable"] = True
    elif "require_invocable" in src:
        out["require_invocable"] = bool(engine.get("require_invocable"))
    if engine.get("accept_payTo_change"):
        out["accept_payTo_change"] = True
    if engine.get("require_transparency"):
        out["require_transparency"] = True
    prefer = src.get("prefer_network")
    if isinstance(prefer, str) and prefer.strip():
        out["prefer_network"] = prefer.strip().lower()
    return out


def attach_policy(result: dict, body: dict | None) -> dict:
    src = body if isinstance(body, dict) else {}
    text = ""
    if isinstance(src.get("policy"), str) and src.get("policy").strip():
        text = src.get("policy")
    elif isinstance(src.get("need"), str):
        text = src.get("need")
    compiled = compile_policy(text)
    cons = merge_constraints(src, compiled)
    applied = public_applied_constraints(cons, src)
    # interpreted_constraints must show constraints actually used, including
    # structured body keys. It previously stayed {} when only networks/etc.
    # were supplied.
    result["interpreted_constraints"] = dict(applied)
    result["applied_constraints"] = dict(applied)
    result["unresolved_constraints"] = compiled["unresolved_constraints"]
    return result
