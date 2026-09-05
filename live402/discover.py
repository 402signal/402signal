"""Public discovery docs for x402scan, Ontario, pay-skills, GoPlausible."""

from __future__ import annotations

from live402 import payment, probe, schema_fields

ORIGIN = "https://402signal.com"
ROUTE = f"{ORIGIN}/route"
DESC = payment.CATALOG_DESCRIPTION
ROUTING_PRICE_USDC = payment.AMOUNT_USD.removeprefix("$")
# Catalog indexes copy DESC. OpenAPI info keeps the short operational remainder.
OPENAPI_INFO_DESCRIPTION = (
    DESC
    + " Authorize $0.003 USDC on Base, Solana, or Algorand. It settles only for a "
    "valid live eligible route backed by a current payment envelope; normal typed misses are not settled. "
    "Seller payment is separate. Reachable 200s are misses. "
    "MCP: GET /mcp.json."
)
GUIDANCE = (
    "POST /route with JSON {need and/or url}. Unpaid calls return HTTP 402. "
    "Agents that intend to pay should POST, not GET. "
    "GET /route with Accept: application/json (or no Accept) returns the 402 "
    "challenge so crawlers can index payment; browsers that send text/html "
    "get a human page. "
    "Wallet checklist: the routing authorization is 3000 atomic USDC ($0.003); "
    "include extra.feePayer on Solana and Algorand; "
    "retry the 402 with PAYMENT-SIGNATURE on POST, never GET; "
    "v1 top-level network is 'base', v2 accepts[].network is CAIP-2 eip155:8453; "
    "copy the target facilitator URL from accepts[].extra.facilitator — do not default to x402.org. "
    "Authorize $0.003 USDC then retry with PAYMENT-SIGNATURE or X-PAYMENT. "
    "We verify, probe, and settle only a valid live eligible route. "
    "Live means a parseable unpaid 402 (PAYMENT-REQUIRED or JSON accepts[]/x402Version), "
    "not merely reachable. HTTP 200 after pay is a live URL plus target contract; "
    "HTTP 503 has three fail-closed outcomes: a normal typed miss has "
    "billing.settlement_state=not_attempted; a required-transparency failure after "
    "settlement has settlement_state=settled; and an ambiguous settlement has "
    "settlement_state=unknown. Inspect billing before retrying, and never reuse an "
    "authorization whose settlement state is unknown. Seller payment is separate. "
    "If inputSchema is missing, live may still be true with invocable:false and miss_reason no_input_schema. "
    "GET /mcp.json lists the MCP route tool (type mcp, toolName route); "
    "POST /mcp initialize and tools/list need no payment; tools/call route is the paid probe. "
    "GET /preview?need= is a free request-time catalog search (not_probed:true). Optional prefer_network=base|solana|algorand is a weak ranking preference (still searches all rails). Optional networks= is a hard policy lock. GET /rails lists pay-in rails. "
    "GET /pulse and GET /dashboard are sample lookups. Pulse discovery copy is hybrid: "
    "current upstream catalogs plus a local shadow catalog. index_status is "
    "upstream-live, shadow-warm, both, or fixture. Pulse does not publish listing totals. "
    "GET /health is {ok:true} only. "
    "POST /validate {url} (or GET /validate?url=) is an unpaid seller probe: agent-ready? Fail-closed SSRF, not a /route paywall bypass. "
    "GET /attestation is a public sha256 of a recent 402signal_observed probe batch (not on-chain). "
    "GET /pq/log/checkpoint and /pq/log/tile/* are an experimental C2SP transparency log. "
    "Production transparency log identity targets Algorand MainNet. "
    "MainNet broadcasting is controlled by runtime policy; confirmed anchors are "
    "published in the public trust descriptor. "
    "Signer never reads BROADCAST and never POSTs. /route does not wait for chain. "
    "Falcon authorizes a checkpoint txn, not a merchant payment. "
    "A settled HTTP 200 winner is not atomic with log append (SEC-ROUTER-004 / A-14). "
    "A free typed miss creates no route-decision leaf. A settled winner does not "
    "require a durable signed leaf unless require_transparency or require_route_binding is true. "
    "Optional require_route_binding=true requests a signed v4 binding for buyer-side "
    "comparison with the current seller challenge before signing. Ordinary requests "
    "keep the v3 receipt path. Guide: https://402signal.com/developers#route-binding. "
    "logged_uncheckpointed is never success "
    "when require_transparency is set. "
    + schema_fields.TRANSPARENCY_RETENTION_DESC
    + " "
    "Probe budget is under 60s; a hang returns 503 JSON with miss_reason probe_timeout. "
    "If ranked candidates remain when the budget ends, miss_reason is probe_budget_exhausted "
    "(not no_candidates). If the request probe ceiling is hit with ranked candidates still untested and "
    "budget remaining, miss_reason/stop_reason is probe_limit_reached (not no_candidates). "
    "Typical probe plan is a first tranche of 3, then 2–4 more if no winner. Hard ceiling is 20. "
    "GET /preview adds discovery_matches, displayed, and a read-only "
    "observation from 402signal_observed history (not_yet_observed when never probed). "
    "Upstream probe is GET first, then POST {} only when GET is 405/501 AND the "
    "catalog explicitly declares POST AND does not require a request body. "
    "Never POST {} after GET 200/400/401/403/404/500. "
    "Never POST seller-declared or catalog-declared input bodies. If a required body "
    "means a valid unpaid probe cannot be constructed, miss_reason is unsafe_to_probe. "
    "DNS uses a bounded getaddrinfo pool (2s) and the TCP/TLS "
    "connection is pinned to those SSRF-checked public IPs with TLS SNI and HTTP Host "
    "set to the original hostname (re-pinned on each redirect hop)."
)

def _origin_from_resource(resource_url: str) -> str:
    raw = (resource_url or ROUTE).strip()
    if raw.endswith("/route"):
        return raw[: -len("/route")] or ORIGIN
    return ORIGIN


def well_known(resource_url: str = ROUTE) -> dict:
    """Bazaar-ish discovery blob. Same body for /.well-known/x402 and .json."""
    required = payment.payment_required(resource_url)
    origin = _origin_from_resource(resource_url)
    accepts = list(required.get("accepts") or [])
    return {
        "version": 1,
        "x402Version": 2,
        "name": "402Signal",
        "description": DESC,
        "homepage": ORIGIN,
        "site": ORIGIN,
        "openapi": f"{origin}/openapi.json",
        "mcp": f"{origin}/mcp.json",
        "mcpEndpoint": f"{origin}/mcp",
        "accepts_payment": True,
        "payment_protocols": ["x402"],
        "default_network": "eip155:8453",
        "default_asset": payment.USDC_BASE,
        "price_usdc": ROUTING_PRICE_USDC,
        "price_atomic": int(payment.AMOUNT_ATOMIC),
        "billing": dict(required.get("billing") or {}),
        "resource": resource_url,
        "resources": [
            "POST /route",
            {
                "url": resource_url,
                "method": "POST",
                "type": "http",
                "description": DESC,
                "mimeType": "application/json",
                "serviceName": "402Signal",
                "price": payment.AMOUNT_USD,
                "price_usdc": ROUTING_PRICE_USDC,
                "price_atomic": payment.AMOUNT_ATOMIC,
                "billing": dict(required.get("billing") or {}),
                "networks": ["eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="],
                "accepts": accepts,
                "extensions": required.get("extensions") or {"bazaar": payment.BAZAAR_EXTENSION},
            },
        ],
        "accepts": accepts,
        "extensions": required.get("extensions") or {"bazaar": payment.BAZAAR_EXTENSION},
        "ownershipProofs": [
            payment.payto_address(),
            payment.payto_solana(),
            payment.payto_algorand(),
        ],
        "payTo": {
            "base": payment.payto_address(),
            "solana": payment.payto_solana(),
            "algorand": payment.payto_algorand(),
        },
        "pay_to": payment.payto_address(),
    }


def openapi_spec(resource_url: str = ROUTE) -> dict:
    """OpenAPI 3.1. Paid POST /route documents HTTP 402 + x-payment-info."""
    origin = _origin_from_resource(resource_url)
    required = payment.payment_required(resource_url)
    miss_enum = list(schema_fields.MISS_REASONS)
    probe_item = {
        "type": "object",
        "properties": {
            "method": {"type": "string"},
            "status": {"type": ["integer", "null"]},
            "miss_reason": {"type": "string", "enum": miss_enum},
        },
    }
    target_schema = {
        "type": "object",
        "properties": {
            "method": {"type": "string"},
            "inputSchema": schema_fields.seller_schema_field(),
            "outputSchema": schema_fields.seller_schema_field(),
            "accepts": {"type": "array", "items": {"type": "object"}},
            "facilitator": {"type": ["string", "null"]},
            "amountAtomic": {"type": ["string", "null"]},
            "displayAmount": {"type": ["string", "null"]},
            "timeoutSeconds": {"type": "integer"},
        },
    }
    live_schema = {
        "type": "object",
        "required": ["billing"],
        "properties": {
            "live": {"type": "boolean"},
            "challenge_observed": {"type": "boolean"},
            "payable": {"type": "boolean"},
            "invocable": {"type": "boolean"},
            "selected_payment": {
                "type": ["object", "null"],
                "description": (
                    "Exact CURRENT OBSERVED payment option that won this route. "
                    "Never a catalog-only rail."
                ),
                "properties": {
                    "rail": {"type": ["string", "null"]},
                    "network": {"type": ["string", "null"]},
                    "asset": {"type": ["string", "null"]},
                    "amount_atomic": {"type": ["integer", "null"]},
                    "display_amount": {"type": ["string", "null"]},
                    "normalized_usd": {"type": ["number", "null"]},
                    "payTo": {"type": ["string", "null"]},
                    "facilitator": {"type": ["string", "null"]},
                },
            },
            "billing": {
                "type": "object",
                "description": "402Signal routing-fee outcome. Seller payment is separate.",
                "properties": {
                    "model": {"type": "string", "const": payment.ROUTING_BILLING_MODEL},
                    "condition": {"type": "string", "const": payment.ROUTING_SETTLEMENT_CONDITION},
                    "asset": {"type": "string", "const": "USDC"},
                    "amount_atomic": {"type": "string", "const": payment.AMOUNT_ATOMIC},
                    "display_amount": {"type": "string", "const": payment.AMOUNT_USD},
                    "rail": {"type": "string", "enum": ["base", "solana", "algorand"]},
                    "settlement_attempted": {"type": ["boolean", "null"]},
                    "settled": {"type": ["boolean", "null"]},
                    "settlement_state": {
                        "type": "string",
                        "enum": ["settled", "not_attempted", "rejected", "unknown"],
                        "description": "Inspect before retrying. unknown means do not reuse this authorization.",
                    },
                },
                "required": [
                    "model", "condition", "asset", "amount_atomic", "display_amount",
                    "rail", "settlement_attempted", "settled", "settlement_state",
                ],
            },
            "changes": {
                "type": "object",
                "properties": {
                    "payTo_changed_at": {"type": ["string", "integer", "null"]},
                    "price_changed_at": {"type": ["string", "integer", "null"]},
                    "schema_changed_at": {"type": ["string", "integer", "null"]},
                },
            },
            "url": {"type": ["string", "null"]},
            "status": {"type": ["integer", "null"]},
            "latency_ms": {"type": ["integer", "null"]},
            "has_402_challenge": {"type": "boolean"},
            "probed_at": {"type": "string"},
            "tried": {"type": "integer"},
            "discovery_matches": {"type": "integer"},
            "candidates_discovered": {"type": "integer"},
            "candidates_considered": {"type": "integer"},
            "candidates_probed": {"type": "integer"},
            "probe_ceiling": {
                "type": "integer",
                "description": "Per-request probe cap (typical 7, thorough 15, hard server ceiling 20).",
            },
            "probe_budget_exhausted": {"type": "boolean"},
            "interpreted_constraints": {
                "type": "object",
                "description": (
                    "Constraints actually used by the engine (structured body keys plus "
                    "compiled NL that reached selection). Empty only when unconstrained."
                ),
            },
            "applied_constraints": {
                "type": "object",
                "description": "Same echo as interpreted_constraints: bounds actually applied.",
            },
            "unmet_constraints": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Named bounds that failed among evaluated live candidates. "
                    "Tiny-price and high-min-observation misses stay distinct."
                ),
            },
            "unresolved_constraints": {"type": "array"},
            "candidate_evaluation_complete": {
                "type": "boolean",
                "description": (
                    "True iff every ranked/need-matching candidate in this request's "
                    "working set was probed. Does not imply global catalog completeness."
                ),
            },
            "evaluation_complete": {
                "type": "boolean",
                "description": "Alias of candidate_evaluation_complete.",
            },
            "discovered_count": {"type": "integer"},
            "probed_count": {"type": "integer"},
            "unprobed_count": {"type": "integer"},
            "stop_reason": {
                "type": "string",
                "enum": [
                    "winner_selected",
                    "candidate_set_exhausted",
                    "probe_limit_reached",
                    "probe_budget_exhausted",
                    "constraints_unmet",
                ],
                "description": (
                    "Why this request stopped probing. winner_selected may leave "
                    "ranked candidates untested (candidate_evaluation_complete=false). "
                    "probe_limit_reached means this request's probe_ceiling was hit "
                    "with untested ranked candidates remaining and the 55s budget still open."
                ),
            },
            "payTo": {"type": ["string", "null"]},
            "payTo_changed": {"type": "boolean"},
            "verified_at": {"type": ["string", "null"]},
            "verified_seconds_ago": {"type": ["integer", "null"]},
            "readiness": {"type": "string", "enum": ["discovered", "payable", "invocable", "recently_verified"]},
            "risk": {"type": "array", "items": {"type": "string"}},
            "history": {
                "type": "object",
                "properties": {
                    "success_24h": {"type": ["number", "null"]},
                    "success_7d": {"type": ["number", "null"]},
                    "n_24h": {"type": ["integer", "null"]},
                    "n_7d": {"type": ["integer", "null"]},
                    "p50_latency_ms": {"type": ["integer", "null"]},
                    "p95_latency_ms": {"type": ["integer", "null"]},
                },
            },
            "traction": {"type": "string"},
            "miss_reason": {"type": "string", "enum": miss_enum},
            "schema_source": {"type": ["string", "null"], "enum": ["envelope", "catalog", "bazaar"]},
            "claimed": schema_fields.claimed_output_schema(),
            "target": target_schema,
            "probes": {"type": "array", "items": probe_item},
            "health": {
                "type": "object",
                "properties": {
                    "live": {"type": "boolean"},
                    "last_probe": {"type": "string"},
                    "latency_ms": {"type": ["integer", "null"]},
                    "has_402_challenge": {"type": "boolean"},
                    "status": {"type": ["integer", "null"]},
                },
            },
            "reputation": {
                "type": "object",
                "description": (
                    "Transparent components first (observed, usage, tenure, stability, "
                    "source_count), then V1 reputation_score, reputation_confidence, "
                    "and scoring_model_id/hash. Score is never returned without components. "
                    "No public 0-100 catalog badge. Unique payer addresses are never listed."
                ),
            },
            "payment_authorization": {
                "type": "object",
                "properties": {
                    "pq_native": {
                        "type": "boolean",
                        "description": "Always false. x402 pay-in is not a Falcon authorization.",
                    }
                },
            },
            "decision_binding": schema_fields.decision_binding_schema(),
            "binding_error": {"type": "string", "enum": ["route_binding_unavailable"]},
            "pq_trust": {
                "type": "object",
                "description": (
                    "Optional experimental transparency receipt. Not atomic with a settled "
                    "winner (SEC-ROUTER-004 / A-14): settlement does not require a "
                    "durable signed leaf unless require_transparency or require_route_binding is true. Free typed "
                    "misses create no route-decision leaf. status is "
                    "pending (durable leaf + signed checkpoint, not MainNet-anchored), "
                    "logged_uncheckpointed (durable leaf, no signed checkpoint), or "
                    "unavailable (receipt unavailable; append may have occurred). logged_uncheckpointed is never success "
                    "when require_transparency is true. "
                    + schema_fields.TRANSPARENCY_RETENTION_DESC
                    + " Not a /trust page."
                ),
                "properties": {
                    "transparency": {
                        "type": "object",
                        "properties": {
                            "status": {
                                "type": "string",
                                "enum": list(schema_fields.TRANSPARENCY_STATUSES),
                            },
                            "state": {
                                "type": "string",
                                "enum": list(schema_fields.TRANSPARENCY_STATES),
                            },
                            "log_origin": {"type": "string"},
                            "leaf_type": {"type": "string"},
                            "index": {"type": "integer"},
                            "checkpoint_size": {"type": "integer"},
                            "receipt": {
                                "type": "object",
                                "description": (
                                    "Inclusion proof and signed checkpoint. Retain it with "
                                    "reveal for later verification."
                                ),
                            },
                            "reveal": {
                                "type": "object",
                                "description": (
                                    "Customer-private routing evidence and salt. Not published "
                                    "in the public log. Private replay outcomes may retain it; "
                                    "keep securely with receipt."
                                ),
                            },
                        },
                    }
                },
            },
            "objective": {
                "type": "string",
                "enum": [
                    "best",
                    "cheapest",
                    "fastest",
                    "most_reliable",
                    "lowest_total_cost",
                    "fastest_settlement",
                ],
            },
            "compared": {
                "type": "array",
                "description": (
                    "Slim probe rows (cap 5). The winner always occupies a slot. "
                    "success_7d is null when n_7d < 3, never an invented 0.0. "
                    "n_7d distinguishes 3/3 from 400/400."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "url": {"type": ["string", "null"]},
                        "rail": {"type": ["string", "null"]},
                        "amount_atomic": {"type": ["integer", "null"]},
                        "latency_ms": {"type": ["integer", "null"]},
                        "success_7d": {"type": ["number", "null"]},
                        "n_7d": {"type": "integer"},
                        "readiness": {"type": ["string", "null"]},
                        "live": {"type": "boolean"},
                        "invocable": {"type": "boolean"},
                        "selected": {"type": "boolean"},
                        "selected_payment": {"type": ["object", "null"]},
                        "reputation": {"type": ["object", "null"]},
                        "economics": {
                            "type": ["object", "null"],
                            "description": (
                                "Rail economics for the selected_payment option. Every field "
                                "has provenance: 402signal_observed, protocol_reference, or unknown."
                            ),
                        },
                    },
                },
            },
        },
    }
    route_body = schema_fields.route_body_schema()
    route_body["properties"]["need"]["example"] = "erc20 token balance"
    route_body["properties"]["url"]["example"] = "https://example.com/x402/balance"
    example_402 = dict(required)
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "402Signal",
            "version": "0.5.0",
            "description": OPENAPI_INFO_DESCRIPTION,
            "x-guidance": GUIDANCE,
            "contact": {"url": ORIGIN, "name": "402Signal", "email": "ross@402signal.com"},
        },
        "servers": [{"url": origin, "description": "This origin"}],
        "tags": [
            {"name": "Paid", "description": "x402-gated routes"},
            {"name": "Public", "description": "Catalog, preflight, rails, and liveness"},
        ],
        "paths": {
            "/route": {
                "get": {
                    "operationId": "getRoute",
                    "tags": ["Paid"],
                    "summary": "Get JSON 402 challenge or HTML page",
                    "description": "Agents and crawlers that omit Accept or send application/json receive HTTP 402 with accepts[]. Browsers that send text/html receive an HTML page. Agents that intend to pay should POST.",
                    "parameters": [
                        {
                            "in": "header",
                            "name": "Accept",
                            "required": False,
                            "description": "text/html returns HTML; application/json or omitted returns HTTP 402 with accepts[].",
                            "schema": {"type": "string", "example": "application/json"},
                        }
                    ],
                    "x-payment-info": {
                        "price": {"mode": "fixed", "currency": "USD", "amount": ROUTING_PRICE_USDC},
                        "protocols": [{"x402": {}}],
                        "networks": ["eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="],
                        "asset": "USDC",
                        "amountAtomic": payment.AMOUNT_ATOMIC,
                        "billingModel": payment.ROUTING_BILLING_MODEL,
                        "settlementCondition": payment.ROUTING_SETTLEMENT_CONDITION,
                        "typedMissesSettled": False,
                    },
                    "responses": {
                        "200": {
                            "description": "HTML page for browsers that send Accept: text/html",
                            "content": {
                                "text/html": {
                                    "schema": {"type": "string"},
                                }
                            },
                        },
                        "402": {
                            "description": "Payment challenge for agents and crawlers. JSON body includes accepts[].",
                            "headers": {
                                "PAYMENT-REQUIRED": {
                                    "description": "Base64 x402 PaymentRequired (v2)",
                                    "schema": {"type": "string"},
                                }
                            },
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/PaymentRequired"},
                                    "example": example_402,
                                }
                            },
                        },
                    },
                },
                "post": {
                    "operationId": "route",
                    "tags": ["Paid"],
                    "summary": "Authorize $0.003 USDC; settle only for a valid live route",
                    "description": DESC,
                    "x-payment-info": {
                        "price": {"mode": "fixed", "currency": "USD", "amount": ROUTING_PRICE_USDC},
                        "protocols": [{"x402": {}}],
                        "networks": ["eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="],
                        "asset": "USDC",
                        "amountAtomic": payment.AMOUNT_ATOMIC,
                        "billingModel": payment.ROUTING_BILLING_MODEL,
                        "settlementCondition": payment.ROUTING_SETTLEMENT_CONDITION,
                        "typedMissesSettled": False,
                    },
                    "x-discovery": {
                        "ownershipProofs": [
                            payment.payto_address(),
                            payment.payto_solana(),
                            payment.payto_algorand(),
                        ]
                    },
                    "security": [{"paymentSignature": []}, {"xPayment": []}],
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": route_body,
                                "example": {
                                    "need": "erc20 token balance",
                                    "url": "https://example.com/x402/balance",
                                },
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Valid live eligible route found and routing authorization settled",
                            "content": {
                                "application/json": {
                                    "schema": live_schema,
                                    "example": {
                                        "live": True,
                                        "invocable": True,
                                        "url": "https://example.com/x402/balance",
                                        "status": 402,
                                        "latency_ms": 87,
                                        "has_402_challenge": True,
                                        "probed_at": "2026-08-29T22:00:00-04:00",
                                        "tried": 1,
                                        "probes": [{"method": "GET", "status": 402}],
                                        "billing": {
                                            "model": payment.ROUTING_BILLING_MODEL,
                                            "condition": payment.ROUTING_SETTLEMENT_CONDITION,
                                            "asset": "USDC",
                                            "amount_atomic": payment.AMOUNT_ATOMIC,
                                            "display_amount": payment.AMOUNT_USD,
                                            "rail": "base",
                                            "settlement_attempted": True,
                                            "settled": True,
                                            "settlement_state": "settled",
                                        },
                                        "target": {
                                            "method": "POST",
                                            "inputSchema": {
                                                "type": "object",
                                                "properties": {"address": {"type": "string"}},
                                                "required": ["address"],
                                            },
                                            "outputSchema": {"type": "object"},
                                            "accepts": [],
                                            "facilitator": "https://api.cdp.coinbase.com/platform/v2/x402",
                                            "amountAtomic": "10000",
                                            "displayAmount": "$0.01",
                                            "timeoutSeconds": 60,
                                        },
                                    },
                                }
                            },
                        },
                        "402": {
                            "description": "Authorize $0.003 USDC (3000 atomic). It settles only for a valid live eligible route; seller payment is separate.",
                            "headers": {
                                "PAYMENT-REQUIRED": {
                                    "description": "Base64 x402 PaymentRequired (v2)",
                                    "schema": {"type": "string"},
                                }
                            },
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/PaymentRequired"},
                                    "example": example_402,
                                }
                            },
                        },
                        "503": {
                            "description": "Fail-closed route outcome. Inspect billing before retrying: a normal typed miss is not attempted, a required-transparency failure can already be settled, and an ambiguous settlement is unknown and must not reuse the authorization.",
                            "content": {
                                "application/json": {
                                    "schema": live_schema,
                                    "examples": {
                                        "normal_typed_miss": {
                                            "summary": "Free normal miss",
                                            "value": {
                                                "live": False,
                                                "invocable": False,
                                                "url": None,
                                                "tried": 0,
                                                "miss_reason": "no_candidates",
                                                "billing": {
                                                    "model": payment.ROUTING_BILLING_MODEL,
                                                    "condition": payment.ROUTING_SETTLEMENT_CONDITION,
                                                    "asset": "USDC",
                                                    "amount_atomic": payment.AMOUNT_ATOMIC,
                                                    "display_amount": payment.AMOUNT_USD,
                                                    "rail": "base",
                                                    "settlement_attempted": False,
                                                    "settled": False,
                                                    "settlement_state": "not_attempted",
                                                },
                                                "probes": [],
                                            },
                                        },
                                        "settled_transparency_failure": {
                                            "summary": "Routing fee settled; required transparency unavailable",
                                            "value": {
                                                "error": "transparency receipt unavailable",
                                                "live": False,
                                                "invocable": False,
                                                "billing": {
                                                    "model": payment.ROUTING_BILLING_MODEL,
                                                    "condition": payment.ROUTING_SETTLEMENT_CONDITION,
                                                    "asset": "USDC",
                                                    "amount_atomic": payment.AMOUNT_ATOMIC,
                                                    "display_amount": payment.AMOUNT_USD,
                                                    "rail": "base",
                                                    "settlement_attempted": True,
                                                    "settled": True,
                                                    "settlement_state": "settled",
                                                },
                                            },
                                        },
                                        "settlement_unknown": {
                                            "summary": "Settlement POST outcome ambiguous; do not retry authorization",
                                            "value": {
                                                "error": "Routing authorization outcome unknown; do not retry this authorization",
                                                "live": False,
                                                "invocable": False,
                                                "miss_reason": "settlement_unknown",
                                                "billing": {
                                                    "model": payment.ROUTING_BILLING_MODEL,
                                                    "condition": payment.ROUTING_SETTLEMENT_CONDITION,
                                                    "asset": "USDC",
                                                    "amount_atomic": payment.AMOUNT_ATOMIC,
                                                    "display_amount": payment.AMOUNT_USD,
                                                    "rail": "base",
                                                    "settlement_attempted": True,
                                                    "settled": None,
                                                    "settlement_state": "unknown",
                                                },
                                            },
                                        },
                                    },
                                }
                            },
                        },
                    },
                }
            },
            "/mcp.json": {
                "get": {
                    "operationId": "mcpManifest",
                    "tags": ["Public"],
                    "summary": "List MCP tools without a payment",
                    "description": "One tool: route. Unpaid tools/call returns HTTP 402.",
                    "responses": {"200": {"description": "MCP manifest"}},
                }
            },
            "/.well-known/mcp.json": {
                "get": {
                    "operationId": "mcpManifestWellKnown",
                    "tags": ["Public"],
                    "summary": "List MCP tools at the well-known path",
                    "responses": {"200": {"description": "MCP manifest"}},
                }
            },
            "/mcp": {
                "get": {
                    "operationId": "mcpManifestAlias",
                    "tags": ["Public"],
                    "summary": "List MCP tools at the MCP alias",
                    "responses": {"200": {"description": "MCP manifest"}},
                },
                "post": {
                    "operationId": "mcpJsonRpc",
                    "tags": ["Paid"],
                    "summary": "Post MCP JSON-RPC; tools/call route is x402-gated",
                    "description": DESC,
                    "x-payment-info": {
                        "price": {"mode": "fixed", "currency": "USD", "amount": ROUTING_PRICE_USDC},
                        "protocols": [{"x402": {}}],
                        "billingModel": payment.ROUTING_BILLING_MODEL,
                        "settlementCondition": payment.ROUTING_SETTLEMENT_CONDITION,
                        "typedMissesSettled": False,
                    },
                    "responses": {
                        "200": {"description": "initialize / tools/list / paid route result"},
                        "402": {"description": "Payment required for tools/call route"},
                    },
                }
            },
            "/preview": {
                "get": {
                    "operationId": "previewNeed",
                    "tags": ["Public"],
                    "summary": "Preview catalog hits without probing them",
                    "description": "Unpaid request-time catalog search. Returns discovery_matches, displayed hits, seller claims, and a read-only 402Signal observation when history exists. not_probed is always true. Does not probe and does not charge. Paid POST /route remains the fail-closed 402 probe. prefer_network is a weak ranking preference (still searches all rails). networks is a hard policy lock.",
                    "parameters": [
                        {
                            "in": "query",
                            "name": "need",
                            "required": True,
                            "schema": {"type": "string", "example": "weather"},
                            "description": "Plain-English lookup to search allowlisted catalogs.",
                        },
                        {
                            "in": "query",
                            "name": "prefer_network",
                            "required": False,
                            "schema": {"type": "string", "enum": ["base", "solana", "algorand"]},
                            "description": "Prefer this pay-in rail when ranking. Searches all supported rails; does not restrict to this rail. Use networks to restrict.",
                        },
                        {
                            "in": "query",
                            "name": "networks",
                            "required": False,
                            "schema": {
                                "type": "array",
                                "items": {"type": "string", "enum": ["base", "solana", "algorand"]},
                            },
                            "style": "form",
                            "explode": True,
                            "description": "Restrict searchable rails to this set. Repeat or comma-separate. Unlike prefer_network, other rails are not queried.",
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Cached hits. not_probed is always true.",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "need": {"type": "string"},
                                            "not_probed": {"type": "boolean"},
                                            "freshness": {"type": ["string", "null"]},
                                            "cached_s": {"type": ["number", "null"]},
                                            "discovery_matches": {"type": "integer"},
                                            "displayed": {"type": "integer"},
                                            "truncated": {"type": "boolean"},
                                            "total": {"type": ["integer", "null"]},
                                            "discovery_via": {
                                                "type": "object",
                                                "additionalProperties": {
                                                    "type": "string",
                                                    "enum": ["search", "pages", "error", "fixture"],
                                                },
                                                "description": "Per-rail how matches were returned. Compact; no internals.",
                                            },
                                            "discovery_exhaustive": {
                                                "type": "boolean",
                                                "description": "True only when every queried rail was untruncated and upstream_total equals returned.",
                                            },
                                            "hits": {
                                                "type": "array",
                                                "description": schema_fields.SELLER_TEXT_CLIENT_WARNING,
                                                "items": schema_fields.preview_hit_schema(),
                                            },
                                            "miss_reason": {"type": "string", "enum": miss_enum},
                                        },
                                    },
                                    "example": {
                                        "need": "weather",
                                        "not_probed": True,
                                        "freshness": "2026-08-30T14:00:00Z",
                                        "discovery_matches": 1,
                                        "displayed": 1,
                                        "hits": [
                                            {
                                                "need": "weather",
                                                "label": "weather",
                                                "url": "https://example.com/x402/weather",
                                                "price": "$0.01",
                                                "chain": "base",
                                                "origin": "catalog_claimed",
                                                "untrusted": True,
                                                "observation": {"status": "not_yet_observed"},
                                            }
                                        ],
                                    },
                                }
                            },
                        }
                    },
                }
            },
            "/rails": {
                "get": {
                    "operationId": "listRails",
                    "tags": ["Public"],
                    "summary": "List pay-in rails with facilitator health",
                    "description": "Three pay-in networks (Base, Solana, Algorand), asset, amountAtomic 3000, facilitators, feePayers, maxTimeoutSeconds, per-rail up+latency. Cached. Not stuffed into /health. Do not default facilitator to x402.org. v1 network is base; v2 accepts[].network is CAIP-2 eip155:8453.",
                    "responses": {
                        "200": {
                            "description": "Pay-in rails snapshot",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "ok": {"type": "boolean"},
                                            "asset": {"type": "string"},
                                            "amountAtomic": {"type": "string"},
                                            "maxTimeoutSeconds": {"type": "integer"},
                                            "facilitators": {"type": "array", "items": {"type": "string"}},
                                            "feePayers": {"type": "object"},
                                            "rails": {"type": "array", "items": {"type": "object"}},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/health": {
                "get": {
                    "operationId": "health",
                    "tags": ["Public"],
                    "summary": "Check service liveness as JSON ok",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"ok": {"type": "boolean"}},
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/pulse": {
                "get": {
                    "operationId": "pulse",
                    "tags": ["Public"],
                    "summary": "Get JSON snapshot of sample lookups",
                    "description": (
                        "Sample lookups and observed facts. Discovery uses current upstream "
                        "catalogs and a local shadow catalog. index_status is upstream-live, "
                        "shadow-warm, both, or fixture. Does not publish listing totals or "
                        "sqlite paths. Rates omitted below n_7d=10. No binary healthy."
                    ),
                    "responses": {
                        "200": {
                            "description": "Public sample lookups snapshot",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "ok": {"type": "boolean"},
                                            "index_status": {
                                                "type": "string",
                                                "enum": [
                                                    "upstream-live",
                                                    "shadow-warm",
                                                    "both",
                                                    "fixture",
                                                ],
                                            },
                                            "observed": {"type": "object"},
                                            "chains": {"type": "object"},
                                            "samples": {"type": "array"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/validate": {
                "get": {
                    "operationId": "validateSellerGet",
                    "tags": ["Public"],
                    "summary": "Ask if a seller URL is agent-ready",
                    "description": "Unpaid seller probe: GET first, then POST {} only if justified. Never POST catalog-declared bodies. DNS IP-pin + fail-closed SSRF. Not a /route payment bypass. Never emits a binary healthy flag.",
                    "parameters": [
                        {
                            "in": "query",
                            "name": "url",
                            "required": True,
                            "schema": {"type": "string", "example": "https://example.com/x402"},
                            "description": "https URL of the seller endpoint to probe.",
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Readiness, claimed vs observed, flags.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ValidateResult"},
                                }
                            },
                        },
                        "400": {"description": "url missing or not https"},
                    },
                },
                "post": {
                    "operationId": "validateSeller",
                    "tags": ["Public"],
                    "summary": "Ask if a seller URL is agent-ready",
                    "description": "Unpaid seller probe: GET first, then POST {} only if justified. Never POST catalog-declared bodies. DNS IP-pin + fail-closed SSRF. Not a /route payment bypass. Never emits a binary healthy flag.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "url": {"type": "string", "example": "https://example.com/x402"},
                                    },
                                    "required": ["url"],
                                    "additionalProperties": False,
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Readiness, claimed vs observed, flags.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ValidateResult"},
                                    "example": {
                                        "url": "https://example.com/x402",
                                        "readiness": "payable",
                                        "live": True,
                                        "payable": True,
                                        "invocable": False,
                                        "claimed": {"payTo": "0xabc", "amount": "10000", "schema_present": None},
                                        "observed": {"payTo": "0xabc", "amount": "10000", "schema_present": None, "http_status": 402, "latency_ms": 41},
                                        "flags": ["missing schema"],
                                        "n_7d": 1,
                                    },
                                }
                            },
                        },
                        "400": {"description": "url missing or not https"},
                    },
                },
            },
            "/pq/log/checkpoint": {
                "get": {
                    "operationId": "pqLogCheckpoint",
                    "tags": ["Public"],
                    "summary": "Experimental C2SP signed checkpoint",
                    "description": (
                        "text/plain C2SP tlog-checkpoint for the current / latest tree. "
                        "GET /pq/log/checkpoint/latest is the same alias. "
                        "Production log identity targets Algorand MainNet. "
                        "MainNet broadcasting is controlled by runtime policy; confirmed "
                        "anchors are published in the public trust descriptor. "
                        "/route does not wait for chain. Falcon authorizes a checkpoint "
                        "txn, not a merchant payment. May be newer than the latest confirmed MainNet anchor."
                    ),
                    "responses": {
                        "200": {"description": "Signed checkpoint note"},
                        "404": {"description": "No checkpoint yet"},
                    },
                }
            },
            "/pq/log/checkpoint/{tree_size}": {
                "get": {
                    "operationId": "pqLogCheckpointAtSize",
                    "tags": ["Public"],
                    "summary": "Experimental C2SP signed checkpoint at a tree size",
                    "description": (
                        "text/plain C2SP tlog-checkpoint for an exact historical tree size. "
                        "GET /pq/log/checkpoint remains the current / latest checkpoint."
                    ),
                    "parameters": [
                        {
                            "in": "path",
                            "name": "tree_size",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1},
                        }
                    ],
                    "responses": {
                        "200": {"description": "Signed checkpoint note for that tree size"},
                        "404": {"description": "No checkpoint at that size"},
                    },
                }
            },
            "/pq/log/tile/{level}/{n}": {
                "get": {
                    "operationId": "pqLogTile",
                    "tags": ["Public"],
                    "summary": "Experimental C2SP Merkle tile",
                    "description": (
                        "application/octet-stream tlog-tiles@v0.1.0. Path is /tile/<L>/<N> "
                        "(height 8 implicit), not sumdb /tile/H/L/N. Partial tiles use .p/<W>. "
                        "Production log identity targets Algorand MainNet. MainNet broadcasting is controlled by runtime policy."
                    ),
                    "parameters": [
                        {"in": "path", "name": "level", "required": True, "schema": {"type": "integer"}},
                        {"in": "path", "name": "n", "required": True, "schema": {"type": "string"}},
                    ],
                    "responses": {
                        "200": {"description": "Tile bytes"},
                        "404": {"description": "Unknown tile"},
                    },
                }
            },
            "/attestation": {
                "get": {
                    "operationId": "attestationHash",
                    "tags": ["Public"],
                    "summary": "Hash a recent observed probe batch",
                    "description": "sha256 of canonical JSON of 402signal_observed rows for a batch_id. Not on-chain. No signatures or keys.",
                    "parameters": [
                        {
                            "in": "query",
                            "name": "batch_id",
                            "required": False,
                            "schema": {"type": "string"},
                            "description": "Optional batch id. Default is the most recent observed batch.",
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Public hash payload.",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "batch_id": {"type": "string"},
                                            "created_at": {"type": ["string", "null"]},
                                            "n": {"type": "integer"},
                                            "algo": {"type": "string"},
                                            "hash": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        },
                        "404": {"description": "No observed batch"},
                    },
                }
            },
            "/dashboard": {
                "get": {
                    "operationId": "dashboard",
                    "tags": ["Public"],
                    "summary": "Render HTML examples of sample lookups",
                    "responses": {"200": {"description": "HTML"}},
                }
            },
        },
        "components": {
            "securitySchemes": {
                "paymentSignature": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "PAYMENT-SIGNATURE",
                    "description": "x402 v2 PaymentPayload, base64 JSON",
                },
                "xPayment": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-PAYMENT",
                    "description": "x402 v1/v2 payment header",
                },
            },
            "schemas": {
                "ValidateResult": {
                    "type": "object",
                    "properties": {
                        "url": {"type": ["string", "null"]},
                        "readiness": {"type": "string", "enum": ["discovered", "payable", "invocable", "recently_verified"]},
                        "live": {"type": "boolean"},
                        "payable": {"type": "boolean"},
                        "invocable": {"type": "boolean"},
                        "claimed": {"type": "object"},
                        "observed": {"type": "object"},
                        "flags": {"type": "array", "items": {"type": "string"}},
                        "n_7d": {"type": "integer"},
                        "miss_reason": {"type": "string"},
                    },
                },
                "PaymentRequired": {
                    "type": "object",
                    "description": "x402 routing authorization. accepts[].amount is 3000 atomic USDC ($0.003); normal typed misses are not settled.",
                    "properties": {
                        "x402Version": {"type": "integer", "example": 2},
                        "error": {"type": "string"},
                        "payTo": {"type": "string"},
                        "network": {"type": "string"},
                        "asset": {"type": "string"},
                        "amount": {"type": "string", "example": payment.AMOUNT_USD},
                        "billing": {"type": "object"},
                        "resource": {"type": "object"},
                        "accepts": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "scheme": {"type": "string"},
                                    "network": {"type": "string"},
                                    "asset": {"type": "string"},
                                    "amount": {
                                        "type": "string",
                                        "description": "Atomic USDC. 3000 = $0.003",
                                        "example": payment.AMOUNT_ATOMIC,
                                    },
                                    "payTo": {"type": "string"},
                                },
                            },
                        },
                        "extensions": {"type": "object"},
                        "help": {"type": "object"},
                    },
                }
            },
        },
        "x-discovery": {
            "ownershipProofs": [
                payment.payto_address(),
                payment.payto_solana(),
                payment.payto_algorand(),
            ]
        },
        "x-examples": {
            "curl": (
                "curl -sS -D - https://402signal.com/route "
                "-H 'Content-Type: application/json' "
                "-d '{\"need\":\"YOUR_NEED\"}'\n"
                "# HTTP 402 + PAYMENT-REQUIRED. Select the matched/observed accept, then:\n"
                "curl -sS https://402signal.com/route "
                "-H 'Content-Type: application/json' "
                "-H \"PAYMENT-SIGNATURE: $SIG\" "
                "-d '{\"need\":\"YOUR_NEED\"}'\n"
                "# HTTP 200 live+target or HTTP 503 miss_reason"
            ),
            "fetch": (
                "const r = await fetch('https://402signal.com/route', "
                "{method:'POST', headers:{'Content-Type':'application/json'}, "
                "body: JSON.stringify({need:'YOUR_NEED'})});\n"
                "// r.status === 402. Sign, then retry:\n"
                "const paid = await fetch('https://402signal.com/route', "
                "{method:'POST', headers:{'Content-Type':'application/json', "
                "'PAYMENT-SIGNATURE': sig}, body: JSON.stringify({need:'YOUR_NEED'})});\n"
                "// paid.status === 200 or 503"
            ),
            "mcp": (
                "POST https://402signal.com/mcp\n"
                '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
                '"params":{"name":"route","arguments":{"need":"YOUR_NEED"}}}\n'
                "# unpaid HTTP 402. Sign, retry the same tools/call with PAYMENT-SIGNATURE. "
                "200 live+target or 503 miss_reason."
            ),
        },
    }


ROBOTS_TXT = """User-agent: *
Allow: /
Allow: /openapi.json
Allow: /.well-known/x402
Allow: /.well-known/x402.json
Allow: /pulse
Allow: /health
Allow: /llms.txt
Allow: /robots.txt
Allow: /mcp
Allow: /mcp.json
Allow: /.well-known/mcp.json
Allow: /preview
Allow: /rails
Allow: /validate
Allow: /attestation
Allow: /pq/log/checkpoint
Allow: /transparency

Sitemap: https://402signal.com/sitemap.xml
"""

LLMS_TXT = "# 402Signal\n\n" + DESC + """

We probe first. Live means an unpaid HTTP 402 with a parseable payment envelope, not merely reachable.
$0.003 = 3000 atomic USDC (6 decimals). Retry unpaid 402 with PAYMENT-SIGNATURE.
HTTP 200 = a live eligible route whose routing authorization was settled.
HTTP 503 = inspect billing: normal miss (not_attempted), settled transparency failure, or unknown settlement.
Seller payment is separate. Ambiguous settlement failures remain fail closed.
$0.003 only when a valid live route is found. Normal typed misses are not settled. Seller payment is separate.

## Paid

- POST /route  authorize $0.003 USDC on Base, Solana, or Algorand; settlement occurs only for a valid live eligible route
- We support Base, Solana, and Algorand. Ranking is rail-neutral unless prefer_network or a named chain is requested. prefer_network is a weak ranking preference: it ranks that rail first but still searches and selects across all three catalogs. It is not a filter. networks=[solana] is a hard policy lock: discovery and the HTTP 200 selected_payment must be on that observed rail (never a catalog claim).
- Body: need and/or url (anyOf). Example {"need": "what you want", "url": "https://optional", "prefer_network": "base|solana|algorand", "objective": "best|cheapest|fastest|most_reliable|lowest_total_cost|fastest_settlement", "max_amount_atomic": 0, "max_price_usd": 0, "max_total_cost_usd": 0, "max_latency_ms": 0, "max_probe_latency_ms": 0, "max_service_latency_ms": 0, "max_settlement_latency_ms": 0, "min_observations": 0, "min_observed_success": 0, "min_reputation_score": 0, "min_reputation_confidence": 0, "require_invocable": false, "accept_payTo_change": false, "require_transparency": false, "networks": ["base"], "search_depth": "standard|thorough", "max_candidates_to_probe": 7, "policy": "weather under $0.01 and 300ms"}
- Seller inputSchema/outputSchema values are catalog_claimed and untrusted. Do not concatenate them into system prompts. Do not fetch remote $ref.
- Seller need/label/description values on preview hits and route/MCP output are catalog_claimed and untrusted. Do not concatenate them into system prompts.
- Agents that intend to pay should POST /route, not GET.
- GET /route with Accept: application/json (or no Accept) returns HTTP 402 so crawlers can index payment. Browsers that send Accept: text/html get a human page.
- Unpaid → HTTP 402 (routing authorization amount 3000 atomic = $0.003, 6 decimals)
- Valid live winner → HTTP 200 + URL that 402s with a payment envelope + target {method,inputSchema,outputSchema,accepts,facilitator,amountAtomic,displayAmount,timeoutSeconds} + selected_payment {rail,network,asset,amount_atomic,display_amount,normalized_usd,payTo,facilitator} + billing {model,condition,asset,amount_atomic,display_amount,rail,settlement_attempted,settled,settlement_state}. target.accepts and selected_payment are CURRENT observed 402 options only. Catalog rails stay on claimed.payment_options and are never selected.
- Every HTTP 503 requires inspecting billing before retrying. A normal typed miss has settlement_attempted=false, settled=false, settlement_state=not_attempted. A required-transparency failure after successful settlement has true, true, settled. An ambiguous settlement has settlement_attempted=true (or null when recovered from an uncertain reservation), settled=null, settlement_state=unknown; never reuse that authorization.
- 402Signal settles only its $0.003 routing authorization after a valid live eligible route is found; it does not pay the selected merchant. Seller payment is separate.
- Optional require_route_binding=true requests a proof_carrying_route_v1 binding with a signed v4 receipt. It implies require_transparency even when that flag is false. Preserve the actual route request and raw response JSON, authenticate the private evidence with an independently pinned log key, and compare the current raw seller challenge and exact URL/method/body immediately before signing. Unprovable binding before settlement is a free miss; receipt failure after settlement remains settled. Ordinary requests retain v3 receipts. Guide: https://402signal.com/developers#route-binding
- Binding supports exact x402 v2 offers on the existing three rails: GET without a body or a justified POST with exactly {}. Disable redirects. Changed terms, unsupported extensions and expired evidence fail closed. The default freshness window is 60 seconds from observation; issuance, replay and human approval never refresh it. A later rejected seller handoff does not undo a settled routing fee. This checks observed terms, not seller delivery or output quality.
- Local Node/TypeScript guard (Node >=22, no runtime dependencies, source only, not published to npm): https://github.com/402signalhq/402signal/tree/main/sdk/route-guard . Python verifier and full contract: https://github.com/402signalhq/402signal/blob/main/docs/proof-carrying-route-v1.md . The immediate receipt uses Ed25519; cumulative Falcon anchoring is separate. The buyer's wallet still enforces transaction validation, budgets and durable economic replay protection. The verifiers do not hold keys, perform network requests or execute payments.
- For later PQ Trust verification, set require_transparency=true and securely retain the complete paid /route response, including compared[]. At minimum, keep pq_trust.transparency.receipt and pq_trust.transparency.reveal together. Private replay outcomes can retain the reveal; they are not a recovery service. Keep your own copy. Modified evidence fails verification against the public log. The reveal contains private request and decision evidence; do not put it in public logs.
- Upstream probe is GET first, then POST {} only with strong method justification (GET 405/501, or the catalog explicitly declares POST and does not require a request body). Never POST {} after an arbitrary 200/400/404. Never POST seller-declared or catalog-declared input bodies. If a required body means a valid unpaid probe cannot be constructed, miss_reason is unsafe_to_probe. DNS uses a bounded getaddrinfo pool (2s); TCP/TLS is pinned to those SSRF-checked public IPs with TLS SNI and HTTP Host set to the original hostname (re-pinned on redirects).
- payable requires a complete observed option (rail/network, amount, asset, payTo). invocable is payable + input schema. challenge_observed is HTTP 402 + parseable x402.
- If inputSchema is missing: live may be true, invocable false, miss_reason no_input_schema
- Normal typed miss → HTTP 503 {live:false, miss_reason, billing:{settlement_attempted:false,settled:false,settlement_state:"not_attempted"}}
- miss_reason enum: no_candidates, no_402_envelope, no_payto, reachable_200, probe_timeout, quote_expired, invalid_need, upstream_5xx, ssrf, no_input_schema, constraints_unmet, probe_budget_exhausted, probe_limit_reached, unsafe_to_probe, settlement_unknown
- Paid /route also returns discovery_matches, candidates_discovered, candidates_considered, candidates_probed, discovered_count, probed_count, unprobed_count, candidate_evaluation_complete, evaluation_complete, probe_ceiling, stop_reason, probe_budget_exhausted, interpreted_constraints, applied_constraints, unmet_constraints, unresolved_constraints. interpreted_constraints / applied_constraints echo constraints actually used (structured body keys included). candidate_evaluation_complete (also evaluation_complete) is true only when every ranked candidate in this request's working set was probed (not the global catalog). stop_reason is winner_selected | candidate_set_exhausted | probe_limit_reached | probe_budget_exhausted | constraints_unmet. Typical probe plan is 3 then +2–4; hard ceiling is 20. probe_limit_reached means ranked candidates remained after this request's probe_ceiling with budget still open; it is not no_candidates. probe_budget_exhausted means ranked candidates remained when the 55s budget ended; it is not no_candidates. max_latency_ms is a probe-RTT alias. Catalog rows stay slim; only top finalists are hydrated with claimed schemas (not observed payment options).
- cheapest / fastest / most_reliable rank the currently probed eligible candidates, not every discovered endpoint. fastest is this-request probe/service RTT, not settlement latency. fastest_settlement is a separate objective (settlement/finality, never probe RTT).
- compared[] rows include success_7d, n_7d, reputation components (and score+confidence+scoring_model_id/hash), and rail economics for the selected_payment option. The winner always appears even when compared is capped. success_7d is null when n_7d < 3. n_7d < 10 means low reputation_confidence and no public reliability %. Unique payer address lists are never returned. lowest_total_cost fails closed when a fee is unknown (merchant price is not total cost). Same scoring function on Base, Solana, and Algorand (no hidden Algorand preference). Vague "high reputation" stays unresolved; "established usage" / "strong observed evidence" compile to min_observations=10. Settlement / total cost compile only with a numeric bound.
- Discovery shortlist keeps need/capability score primary. History only reorders close scores, with freshness bands on prior success (<5m / <1h / <24h / older). A stale 402 cannot leapfrog a substantially better semantic match.
- GET /pulse observed facts are n_7d, success_7d, payable_rate_7d, invocable_rate_7d. Rates are omitted below n=10. No binary healthy. No executable_now_rate.
- POST /mcp tools/call name=route is the same paid probe (unpaid tools/call also 402s)
- MCP bazaar type is mcp, toolName is route. Live MCP: https://402signal.com/mcp and /mcp.json

## Public

- GET /  human homepage
- GET /transparency  first-party PQ transparency read page (confirmed MainNet anchors when present; awaiting first confirmed MainNet checkpoint; routing does not wait for chain)
- GET /dashboard  sample lookups per chain (Base / Solana / Algorand)
- GET /pulse  same snapshot as JSON, including samples[]. index_status is upstream-live | shadow-warm | both | fixture. Discovery queries current upstream catalogs and a local shadow catalog. Pulse does not publish listing totals.
- GET /preview?need=weather  request-time catalog search (current upstream catalogs plus a local shadow; not a full-world RAM index) + discovery_matches + displayed + seller claims + read-only 402Signal observation (not_yet_observed when never independently probed). not_probed:true (does not probe, does not charge). Optional prefer_network=base|solana|algorand ranks across all rails; optional networks=solana restricts rails. discovery_via is a compact per-rail search|pages|error|fixture map. discovery_exhaustive is true only when the returned set is known complete. Catalog rows keep three clocks (discovery, claim, verification); a paid route also returns this request's probe time. HEAD 200 on /llms.txt /openapi.json /mcp.json /preview /rails /pulse.
- POST /validate {"url":"https://seller.example/x402"}  unpaid seller probe (GET first, justified POST {} only, never a catalog-declared body, DNS IP-pin): is this seller agent-ready? Also GET /validate?url=. Fail-closed SSRF. Not a /route paywall bypass. Readiness + claimed vs observed + flags. Never a binary healthy flag.
- GET /attestation  public sha256 of a recent 402signal_observed probe batch (batch_id, created_at, n, algo, hash). Not on-chain. Optional ?batch_id=.
- GET /pq/log/checkpoint (also GET /pq/log/checkpoint/latest) and GET /pq/log/tile/*  experimental C2SP transparency log (tlog-checkpoint + tlog-tiles). Production transparency log identity targets Algorand MainNet. MainNet broadcasting is controlled by runtime policy; confirmed anchors are published in the public trust descriptor. Signer never reads BROADCAST and never POSTs. Falcon SK must never live on 402signal. last_confirmed is persisted only after an independent MainNet fetch+verify. /route does not wait for chain. Falcon authorizes a checkpoint txn, not a merchant payment. A settled winner may include pq_trust.transparency. Settlement and log append are not atomic (SEC-ROUTER-004 / A-14): a settled winner does not require a durable signed leaf unless require_transparency or require_route_binding is true. A free typed miss appends no route-decision leaf and cannot trigger an anchor solely for that request. If required transparency fails after settlement, billing still reports settled=true and no second settlement is attempted. status pending means a durable leaf and a signed checkpoint (state checkpoint_signed), not an Algorand inclusion. logged_uncheckpointed means a durable leaf without a signed checkpoint. unavailable means the receipt is unavailable; an append may have occurred. It is not pending. logged_uncheckpointed is never success when require_transparency is true. Never call a leaf signed if there is no checkpoint. payment_authorization.pq_native is always false. GET /pq/log/trust is the public trust descriptor for the configured epoch (runtime Ed25519 vkey only; env vkey wins over stale sqlite meta.vkey; witness_policy is empty). Homepage PQ card evidence renders only when last_confirmed has a real confirmed txid. GET /transparency is the first-party read page. Default v3 and opt-in v4 public leaves reveal type, minute-rounded ts, nonce, and commitment. They do not reveal the private request evidence, salt, wallet, or payment. Historical versions retain their original verification semantics. A public leaf is not a claim of anonymous or unlinkable traffic.
- GET /rails  three pay-in networks, asset, amountAtomic, facilitators, feePayers, maxTimeoutSeconds, per-rail up+latency
- GET /health  {"ok":true} liveness only
- GET /ready  storage/catalog/history/pq_log/replay-ledger booleans. No paths or secrets. Fly keeps /health for liveness and also requires /ready before routing traffic.
- GET /openapi.json
- GET /.well-known/x402
- GET /.well-known/x402.json
- GET /mcp.json
- GET /mcp  same as /mcp.json
- GET /.well-known/mcp.json
- GET /llms.txt
- GET /robots.txt
- POST /mcp initialize, tools/list, tools/call preview, and tools/call validate (no payment)

## Listed on

- Glama: https://glama.ai/mcp/servers/402signal/402signal
- MCP Registry: https://registry.modelcontextprotocol.io/?q=402signal (io.github.402signalhq/402signal)
- Gold-402: https://github.com/Haustorium12/gold-402/blob/main/directory/aggregators.md
- Smithery: https://smithery.ai/servers/live402/signal
- Agentic Market: https://agentic.market/services/402signal-com
- x402-dev: https://github.com/michielpost/x402-dev/blob/master/Projects.md
- GoPlausible: https://facilitator.goplausible.xyz/dashboard/merchants/56466a9400d70f08
- x402scan: https://www.x402scan.com/recipient/0xb18fc2275f36dae99eb215caeff03b431f887d16

## Discovery (machine)

- CDP: https://api.cdp.coinbase.com/platform/v2/x402/discovery/search?query=402signal
- PayAI: https://facilitator.payai.network/discovery/resources
- GoPlausible: https://facilitator.goplausible.xyz/discovery/resources
- 402index: https://402index.io/api/v1/services/ee14cbd5-19c4-4408-84aa-e465323699b1

## Paid retry

Unpaid 402 → authorize $0.003 → select the matched/observed accept → PAYMENT-SIGNATURE → settled 200 winner or unsettled 503 typed miss.

curl:
  curl -sS -D - https://402signal.com/route -H 'Content-Type: application/json' -d '{"need":"YOUR_NEED"}'
  curl -sS https://402signal.com/route -H 'Content-Type: application/json' -H "PAYMENT-SIGNATURE: $SIG" -d '{"need":"YOUR_NEED"}'

Fetch:
  const r = await fetch("https://402signal.com/route", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({need:"YOUR_NEED"})});
  const paid = await fetch("https://402signal.com/route", {method:"POST", headers:{"Content-Type":"application/json","PAYMENT-SIGNATURE": sig}, body: JSON.stringify({need:"YOUR_NEED"})});

MCP tools/call:
  POST https://402signal.com/mcp
  {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"route","arguments":{"need":"YOUR_NEED"}}}
  unpaid HTTP 402; retry the same body with PAYMENT-SIGNATURE.

Wallet checklist: USDC 6 decimals; include extra.feePayer on Solana/Algorand; POST-not-GET; v1 network is base, v2 accepts[].network is CAIP-2 eip155:8453. Copy the target facilitator from accepts[].extra.facilitator. Do not default to x402.org.

USDC has 6 decimals. The 402Signal routing authorization is 3000 atomic ($0.003). Seller payment is separate.
"""
