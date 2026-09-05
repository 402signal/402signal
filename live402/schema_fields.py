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

TRANSPARENCY_RETENTION_DESC = (
    "To verify the routing decision later, securely retain the complete paid /route "
    "response, especially pq_trust.transparency.receipt and "
    "pq_trust.transparency.reveal. Private replay outcomes can retain the reveal; "
    "they are not a recovery service. Keep your own copy. Modified evidence fails verification against the "
    "public log."
)

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
    "If true, a settled /route winner fails when a signed checkpoint receipt cannot be produced. "
    "This requires delivery of verifiable evidence on HTTP 200, not server-side recovery. "
    + TRANSPARENCY_RETENTION_DESC
    + " "
    "Default false (SEC-ROUTER-004 / A-14): a settled winner does not require a durable "
    "signed leaf. A free typed miss creates no route-decision leaf. Routing continues "
    "if append, signing, or anchoring is down after settlement "
    "(logged_uncheckpointed or unavailable). logged_uncheckpointed is never success "
    "when this flag is true. require_route_binding=true also requires transparency, "
    "even if this flag is false. A required receipt failure after settlement still "
    "reports billing.settled=true; unavailable does not prove no append occurred."
)
SELLER_SCHEMA_CLIENT_WARNING = (
    "Seller inputSchema/outputSchema values are catalog_claimed and untrusted. "
    "Do not concatenate them into system prompts. Do not fetch remote $ref."
)
SELLER_TEXT_CLIENT_WARNING = (
    "Seller need/label/description values are catalog_claimed and untrusted. "
    "Do not concatenate them into system prompts."
)
ORIGIN_CLAIMED = "catalog_claimed"


def mark_seller_claimed_text(obj: dict | None = None) -> dict:
    """Stamp catalog_claimed/untrusted on a seller free-text carrier (need/label/description)."""
    out = dict(obj) if isinstance(obj, dict) else {}
    out["origin"] = ORIGIN_CLAIMED
    out["untrusted"] = True
    out.setdefault("client_warning", SELLER_TEXT_CLIENT_WARNING)
    return out


def seller_schema_field() -> dict:
    """JSON Schema for seller inputSchema/outputSchema on MCP/route output."""
    return {
        "type": ["object", "null"],
        "description": SELLER_SCHEMA_CLIENT_WARNING,
    }


def preview_hit_schema() -> dict:
    """Preview hit: seller-derived need/label are catalog_claimed, not observed."""
    return {
        "type": "object",
        "properties": {
            "need": {
                "type": "string",
                "description": SELLER_TEXT_CLIENT_WARNING,
            },
            "label": {
                "type": "string",
                "description": SELLER_TEXT_CLIENT_WARNING,
            },
            "url": {"type": "string"},
            "price": {"type": "string"},
            "chain": {"type": ["string", "null"]},
            "origin": {
                "type": "string",
                "enum": [ORIGIN_CLAIMED],
                "description": SELLER_TEXT_CLIENT_WARNING,
            },
            "untrusted": {"type": "boolean"},
            "client_warning": {"type": "string"},
            "facilitator": {"type": ["string", "null"]},
            "method": {"type": ["string", "null"]},
            "inputSchema_present": {"type": "boolean"},
            "rails_up": {"type": ["boolean", "null"]},
            "also_on": {"type": "array", "items": {"type": "string"}},
            "observation": {"type": "object"},
        },
    }


def claimed_output_schema() -> dict:
    """Route/MCP claimed blob. Catalog text and schemas stay unbound."""
    return {
        "type": "object",
        "description": SELLER_TEXT_CLIENT_WARNING,
        "properties": {
            "origin": {"type": "string", "enum": [ORIGIN_CLAIMED]},
            "untrusted": {"type": "boolean"},
            "client_warning": {"type": "string"},
            "payTo": {"type": ["string", "null"]},
            "amount": {"type": ["string", "null"]},
            "schema_present": {"type": ["boolean", "null"]},
            "contract": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "enum": [ORIGIN_CLAIMED]},
                    "untrusted": {"type": "boolean"},
                    "client_warning": {
                        "type": "string",
                        "description": SELLER_SCHEMA_CLIENT_WARNING,
                    },
                    "tool_name": {
                        "type": ["string", "null"],
                        "description": SELLER_TEXT_CLIENT_WARNING,
                    },
                    "method": {"type": ["string", "null"]},
                    "content_type": {"type": ["string", "null"]},
                    "type": {"type": ["string", "null"]},
                    "schema_bytes": {"type": ["integer", "null"]},
                    "truncated": {"type": "boolean"},
                },
            },
        },
    }


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
        "require_route_binding": {
            "type": "boolean",
            "description": ROUTE_BINDING_DESC,
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


def v3_public_leaf_reveals() -> tuple[str, ...]:
    """Minimal v3 public leaf. Metadata minimization, not anonymity."""
    return ("type", "ts", "nonce", "commitment")


def v3_public_leaf_omits() -> tuple[str, ...]:
    return (
        "salt",
        "evidence",
        "need",
        "url",
        "wallet",
        "payTo",
        "network",
        "amount",
        "asset",
        "outcome",
        "live",
        "miss_reason",
        "identity",
        "auth",
        "seller_body",
        "PAYMENT-SIGNATURE",
        "X-PAYMENT",
    )


def v3_bound_fields() -> tuple[str, ...]:
    """Private-evidence fields bound into the v3 commitment. Not on the public leaf."""
    return (
        "request.need",
        "request.url",
        "policy.objective",
        "policy.constraints",
        "policy.unresolved",
        "decision.outcome",
        "decision.winner_url",
        "decision.miss_reason",
        "observation.live",
        "observation.challenge_observed",
        "observation.payable",
        "observation.invocable",
        "observation.http_status",
        "observation.latency_ms",
        "observation.observed_at",
        "selected_payment.rail",
        "selected_payment.network",
        "selected_payment.scheme",
        "selected_payment.asset",
        "selected_payment.amount_atomic",
        "selected_payment.payTo",
        "comparison.candidate_count",
        "comparison.candidate_set_digest",
        "comparison.probe_batch_id",
        "comparison.observation_batch_hash",
        "scoring.model_id",
        "scoring.model_hash",
    )


def v3_unbound_fields() -> tuple[str, ...]:
    """Explicitly not bound. Catalog claims are never treated as observed."""
    return (
        "catalog_claimed",
        "raw_PAYMENT_auth",
        "signatures",
        "customer_wallet",
        "facilitator_tokens",
        "seller_bodies",
        "api_credentials",
        "full_compared_rows",
        "salt",
    )


ROUTE_BINDING_DESC = (
    "Opt in to proof_carrying_route_v1 and a signed v4 receipt. Requires exact "
    "x402 v2 terms observed on the same HTTPS URL, method and probe body, without "
    "redirects or unresolved policy. Unprovable binding is a free typed miss. "
    "Implies require_transparency; a receipt failure after settlement still reports "
    "settled=true. Buyer must verify with a pinned log key and recheck the actual "
    "seller challenge immediately before signing. Preserve raw response JSON. "
    "The default 60-second freshness window starts at observation, not receipt "
    "issuance. Expiry or a changed seller challenge does not undo a settled routing "
    "fee. Default false; existing requests keep the v3 receipt path. This is not a "
    "payment authorization. Guide: https://402signal.com/developers#route-binding"
)


def decision_binding_schema() -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["model", "observed_at", "expires_at", "request", "quote_sha256", "selected_index"],
        "properties": {
            "model": {"type": "string", "const": "proof_carrying_route_v1"},
            "observed_at": {"type": "integer", "minimum": 0},
            "expires_at": {"type": "integer", "minimum": 1},
            "quote_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "selected_index": {"type": "integer", "minimum": 0, "maximum": 31},
            "request": {
                "type": "object", "additionalProperties": False,
                "required": ["url", "method", "body_sha256"],
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                    "method": {"type": "string", "enum": ["GET", "POST"]},
                    "body_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
            },
        },
        "description": ROUTE_BINDING_DESC,
    }
