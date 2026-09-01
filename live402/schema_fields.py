"""Shared route/MCP/OpenAPI field constants. One source for schemas and docs."""

from __future__ import annotations

from live402 import probe, select

OBJECTIVES = select.OBJECTIVES
MISS_REASONS = probe.MISS_REASONS
STOP_REASONS = probe.STOP_REASONS
RAILS = ("base", "solana", "algorand")
SEARCH_DEPTHS = tuple(sorted(select.SEARCH_DEPTHS))

TRANSPARENCY_STATES = (
    "logged_uncheckpointed",
    "checkpoint_signed",
    "authorized",
    "submitted",
    "confirmed",
    "unavailable",
)

# Public status kept for clients that still read pending = durable + signed checkpoint.
TRANSPARENCY_STATUSES = ("pending", "logged_uncheckpointed", "unavailable")

NEED_OR_URL_ANYOF = (
    {"required": ["need"]},
    {"required": ["url"]},
)

OBJECTIVE_DESC = (
    "Best-of-N among currently probed eligible candidates, not every discovered "
    "endpoint. cheapest, fastest, and most_reliable rank that probed survivor "
    "set. fastest is this-request probe RTT, not settlement latency. "
    "fastest_settlement is a separate settlement/finality objective. "
    "lowest_total_cost fails closed when a fee is unknown."
)

NEED_DESC = "What the caller wants routed (plain English)."
URL_DESC = "Optional https URL to probe instead of discovery. need or url (or both) is required."
PREFER_NETWORK_DESC = (
    "Weak ranking preference only. Ranks this pay-in rail first but still "
    "searches and selects across all supported rails. Not a filter. "
    "Use networks for a hard policy lock."
)
ACCEPT_PAYTO_CHANGE_DESC = (
    "If true, allow selecting a destination whose payTo just changed for the first time. "
    "Default false: the first unexpected payTo change is not selectable; a second later "
    "independent observation of the same destination can establish it."
)
REQUIRE_TRANSPARENCY_DESC = (
    "If true, paid /route fails when a signed checkpoint receipt cannot be produced. "
    "Default false: routing continues if signing or anchoring is down."
)
SELLER_SCHEMA_CLIENT_WARNING = (
    "Seller inputSchema/outputSchema values are catalog_claimed and untrusted. "
    "Do not concatenate them into system prompts. Do not fetch remote $ref."
)


def need_or_url_schema(*, need_desc: str = NEED_DESC, url_desc: str = URL_DESC) -> dict:
    """JSON Schema fragment: need XOR-or-both url via anyOf."""
    return {
        "need": {"type": "string", "description": need_desc},
        "url": {"type": "string", "description": url_desc},
    }


def route_constraint_properties() -> dict:
    """Structured constraint fields shared by OpenAPI and MCP."""
    return {
        "prefer_network": {
            "type": "string",
            "enum": list(RAILS),
            "description": PREFER_NETWORK_DESC,
        },
        "objective": {
            "type": "string",
            "enum": list(OBJECTIVES),
            "description": OBJECTIVE_DESC,
        },
        "max_amount_atomic": {
            "type": "integer",
            "minimum": 0,
            "description": "Drop live hits whose known atomic amount exceeds this bound. Unknown or cross-asset amount fails closed.",
        },
        "max_price_usd": {
            "type": "number",
            "minimum": 0,
            "description": "Drop live hits whose known normalized USD exceeds this bound. Unknown USD fails closed.",
        },
        "max_latency_ms": {
            "type": "integer",
            "minimum": 0,
            "description": "Compatibility alias for max_probe_latency_ms (this request's probe RTT). Unknown latency fails closed.",
        },
        "max_probe_latency_ms": {
            "type": "integer",
            "minimum": 0,
            "description": "Drop live hits whose known probe RTT exceeds this bound. Not historical service/p50 latency.",
        },
        "max_service_latency_ms": {
            "type": "integer",
            "minimum": 0,
            "description": "Drop live hits whose historical p50 latency exceeds this bound. Unknown p50 fails closed.",
        },
        "require_invocable": {
            "type": "boolean",
            "description": "If true, drop live hits without an input schema.",
        },
        "networks": {
            "type": "array",
            "items": {"type": "string", "enum": list(RAILS)},
            "description": (
                "Hard policy lock. Restricts discovery and selection to this set. "
                "A HTTP 200 winner must have selected_payment.network in this set "
                "from the CURRENT observed 402, never a catalog claim. "
                "Unlike prefer_network, this is not a ranking preference."
            ),
        },
        "min_observations": {
            "type": "integer",
            "minimum": 0,
            "description": "Require history n_7d at least this large. Unknown or smaller fails closed.",
        },
        "min_observed_success": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "Require observed success_7d when n_7d >= 3. Unknown fails closed.",
        },
        "min_reputation_score": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "Require V1 reputation_score. Unknown fails closed. Never guessed from vague NL.",
        },
        "min_reputation_confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "Require reputation_confidence. n_7d < 10 is low confidence.",
        },
        "max_total_cost_usd": {
            "type": "number",
            "minimum": 0,
            "description": "Merchant price plus known fees. Unknown fee fails closed.",
        },
        "max_settlement_latency_ms": {
            "type": "integer",
            "minimum": 0,
            "description": "Settlement/finality bound. Not probe RTT. Unknown fails closed.",
        },
        "search_depth": {
            "type": "string",
            "enum": list(SEARCH_DEPTHS),
            "description": "standard: first 3 then expand 2-4 (typical cap 7). thorough may expand further. Hard server ceiling is 20.",
        },
        "max_candidates_to_probe": {
            "type": "integer",
            "minimum": 1,
            "description": "Requested probe cap, hard-capped at 20.",
        },
        "policy": {
            "type": "string",
            "description": "Natural-language constraints compiled into structured values. Unresolved phrases are returned, never guessed.",
        },
        "accept_payTo_change": {
            "type": "boolean",
            "description": ACCEPT_PAYTO_CHANGE_DESC,
        },
        "require_transparency": {
            "type": "boolean",
            "description": REQUIRE_TRANSPARENCY_DESC,
        },
    }


def route_body_schema() -> dict:
    props = need_or_url_schema()
    props.update(route_constraint_properties())
    return {
        "type": "object",
        "properties": props,
        "anyOf": list(NEED_OR_URL_ANYOF),
        "additionalProperties": False,
    }


def miss_reason_schema() -> dict:
    return {
        "type": ["string", "null"],
        "enum": list(MISS_REASONS) + [None],
    }


def v2_public_leaf_reveals() -> tuple[str, ...]:
    """Fields present on a v2 public leaf. Not a claim of anonymous or unlinkable."""
    return ("type", "ts", "nonce", "commitment", "live", "miss_reason")


def v2_public_leaf_omits() -> tuple[str, ...]:
    return (
        "salt",
        "evidence",
        "need",
        "url",
        "prompt",
        "wallet",
        "payTo",
        "payment",
        "PAYMENT-SIGNATURE",
        "X-PAYMENT",
    )
