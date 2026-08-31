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

# Safety-critical / unmeasured. Never invent a numeric bound from these.
_UNRESOLVED_HINTS = (
    (re.compile(r"\breputat", re.I), "min_reputation_score"),
    (re.compile(r"\btrust\s*score\b", re.I), "min_reputation_score"),
    (re.compile(r"\bsettlement\b", re.I), "max_settlement_latency_ms"),
    (re.compile(r"\bsuccess(?:\s+rate)?\b", re.I), "min_observed_success"),
    (re.compile(r"\b(?:reliable|reliability)\b", re.I), "min_observed_success"),
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
    Reputation, settlement, and success-rate language is unresolved.
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

    for rx, name in _UNRESOLVED_HINTS:
        m = rx.search(raw)
        if m:
            note_unresolved(
                name,
                m.group(0),
                "not measured; not guessed",
            )

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
        "max_settlement_latency_ms",
    ):
        if key in src:
            overlay[key] = src[key]
    cons = select.parse_constraints(overlay)
    return cons


def attach_policy(result: dict, body: dict | None) -> dict:
    src = body if isinstance(body, dict) else {}
    text = ""
    if isinstance(src.get("policy"), str) and src.get("policy").strip():
        text = src.get("policy")
    elif isinstance(src.get("need"), str):
        text = src.get("need")
    compiled = compile_policy(text)
    result["interpreted_constraints"] = compiled["interpreted_constraints"]
    result["unresolved_constraints"] = compiled["unresolved_constraints"]
    return result
