"""Public discovery docs for x402scan, Ontario, pay-skills, GoPlausible."""

from __future__ import annotations

from live402 import payment, probe

ORIGIN = "https://402signal.com"
ROUTE = f"{ORIGIN}/route"
DESC = (
    "Fail-closed live-endpoint x402 router. "
    "Pay $0.01 USDC on Base, Solana, or Algorand for a URL that answers unpaid "
    "HTTP 402 with a payment envelope, or an honest miss. Reachable 200s are misses. "
    "MCP: GET /mcp.json."
)
GUIDANCE = (
    "POST /route with JSON {need, url?}. Unpaid calls return HTTP 402. "
    "Agents that intend to pay should POST, not GET. "
    "GET /route with Accept: application/json (or no Accept) returns the 402 "
    "challenge so crawlers can index payment; browsers that send text/html "
    "get a human page. "
    "Wallet checklist: USDC is 6 decimals (10000 atomic = $0.01); "
    "include extra.feePayer on Solana and Algorand; "
    "retry the 402 with PAYMENT-SIGNATURE on POST, never GET; "
    "v1 top-level network is 'base', v2 accepts[].network is CAIP-2 eip155:8453; "
    "copy the target facilitator URL from accepts[].extra.facilitator — do not default to x402.org. "
    "Pay $0.01 USDC then retry with PAYMENT-SIGNATURE or X-PAYMENT. We verify, probe, then settle. "
    "Live means a parseable unpaid 402 (PAYMENT-REQUIRED or JSON accepts[]/x402Version), "
    "not merely reachable. HTTP 200 after pay is a live URL plus target contract; "
    "503 is an honest miss (same $0.01, typed miss_reason). "
    "If inputSchema is missing, live may still be true with invocable:false and miss_reason no_input_schema. "
    "GET /mcp.json lists the MCP route tool (type mcp, toolName route); "
    "POST /mcp initialize and tools/list need no payment; tools/call route is the paid probe. "
    "GET /preview?need= is a cached preflight (not_probed:true). GET /rails lists pay-in rails. "
    "GET /pulse and GET /dashboard are sample lookups. GET /health is {ok:true} only. "
    "Probe budget is under 60s; a hang returns 503 JSON with miss_reason probe_timeout."
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
        "price_usdc": "0.01",
        "price_atomic": 10000,
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
                "price": "$0.01",
                "price_usdc": "0.01",
                "price_atomic": "10000",
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
    miss_enum = list(probe.MISS_REASONS)
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
            "inputSchema": {"type": ["object", "null"]},
            "outputSchema": {"type": ["object", "null"]},
            "accepts": {"type": "array", "items": {"type": "object"}},
            "facilitator": {"type": ["string", "null"]},
            "amountAtomic": {"type": ["string", "null"]},
            "displayAmount": {"type": ["string", "null"]},
            "timeoutSeconds": {"type": "integer"},
        },
    }
    live_schema = {
        "type": "object",
        "properties": {
            "live": {"type": "boolean"},
            "invocable": {"type": "boolean"},
            "url": {"type": ["string", "null"]},
            "status": {"type": ["integer", "null"]},
            "latency_ms": {"type": ["integer", "null"]},
            "has_402_challenge": {"type": "boolean"},
            "probed_at": {"type": "string"},
            "tried": {"type": "integer"},
            "payTo": {"type": ["string", "null"]},
            "payTo_changed": {"type": "boolean"},
            "traction": {"type": "string"},
            "miss_reason": {"type": "string", "enum": miss_enum},
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
        },
    }
    route_body = {
        "type": "object",
        "properties": {
            "need": {
                "type": "string",
                "description": "What the caller wants routed (plain English).",
                "example": "erc20 token balance",
            },
            "url": {
                "type": "string",
                "description": "Optional https URL to probe instead of discovery.",
                "example": "https://example.com/x402/balance",
            },
        },
        "required": ["need"],
        "additionalProperties": False,
    }
    example_402 = dict(required)
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "402Signal",
            "version": "0.4.0",
            "description": DESC,
            "x-guidance": GUIDANCE,
            "contact": {"url": ORIGIN, "name": "402Signal", "email": "402signal@gmail.com"},
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
                        "price": {"mode": "fixed", "currency": "USD", "amount": "0.01"},
                        "protocols": [{"x402": {}}],
                        "networks": ["eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="],
                        "asset": "USDC",
                        "amountAtomic": "10000",
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
                    "summary": "Pay $0.01 USDC for a live paid-API URL or an honest miss",
                    "description": DESC,
                    "x-payment-info": {
                        "price": {"mode": "fixed", "currency": "USD", "amount": "0.01"},
                        "protocols": [{"x402": {}}],
                        "networks": ["eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="],
                        "asset": "USDC",
                        "amountAtomic": "10000",
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
                            "description": "Live URL found after payment",
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
                            "description": "Payment required. $0.01 USDC. Protocol amount is 10000 atomic (6 decimals), not 10,000 dollars.",
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
                            "description": "Honest miss. Paid probe found nothing live.",
                            "content": {
                                "application/json": {
                                    "schema": live_schema,
                                    "example": {
                                        "live": False,
                                        "invocable": False,
                                        "url": None,
                                        "tried": 0,
                                        "miss_reason": "no_candidates",
                                        "probes": [],
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
                        "price": {"mode": "fixed", "currency": "USD", "amount": "0.01"},
                        "protocols": [{"x402": {}}],
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
                    "summary": "Preview cached catalog hits without probing",
                    "description": "Unpaid preflight from /pulse cache. Returns hits, prices, freshness, not_probed:true. Does not probe and does not charge. Paid POST /route remains the fail-closed 402 probe.",
                    "parameters": [
                        {
                            "in": "query",
                            "name": "need",
                            "required": True,
                            "schema": {"type": "string", "example": "weather"},
                            "description": "Plain-English lookup to match against cached samples.",
                        }
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
                                            "hits": {"type": "array"},
                                            "miss_reason": {"type": "string", "enum": miss_enum},
                                        },
                                    },
                                    "example": {
                                        "need": "weather",
                                        "not_probed": True,
                                        "freshness": "2026-08-30T14:00:00Z",
                                        "hits": [
                                            {
                                                "need": "weather",
                                                "url": "https://example.com/x402/weather",
                                                "price": "$0.01",
                                                "chain": "base",
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
                    "description": "Three pay-in networks (Base, Solana, Algorand), asset, amountAtomic 10000, facilitators, feePayers, maxTimeoutSeconds, per-rail up+latency. Cached. Not stuffed into /health. Do not default facilitator to x402.org. v1 network is base; v2 accepts[].network is CAIP-2 eip155:8453.",
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
                    "responses": {"200": {"description": "Public sample lookups snapshot"}},
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
                "PaymentRequired": {
                    "type": "object",
                    "description": "x402 PaymentRequired. accepts[].amount is atomic USDC (10000 = $0.01).",
                    "properties": {
                        "x402Version": {"type": "integer", "example": 2},
                        "error": {"type": "string"},
                        "payTo": {"type": "string"},
                        "network": {"type": "string"},
                        "asset": {"type": "string"},
                        "amount": {"type": "string", "example": "$0.01"},
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
                                        "description": "Atomic USDC. 10000 = $0.01",
                                        "example": "10000",
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
                "-d '{\"need\":\"weather\"}'\n"
                "# HTTP 402 + PAYMENT-REQUIRED. Sign accepts[0], then:\n"
                "curl -sS https://402signal.com/route "
                "-H 'Content-Type: application/json' "
                "-H \"PAYMENT-SIGNATURE: $SIG\" "
                "-d '{\"need\":\"weather\"}'\n"
                "# HTTP 200 live+target or HTTP 503 miss_reason"
            ),
            "fetch": (
                "const r = await fetch('https://402signal.com/route', "
                "{method:'POST', headers:{'Content-Type':'application/json'}, "
                "body: JSON.stringify({need:'weather'})});\n"
                "// r.status === 402. Sign, then retry:\n"
                "const paid = await fetch('https://402signal.com/route', "
                "{method:'POST', headers:{'Content-Type':'application/json', "
                "'PAYMENT-SIGNATURE': sig}, body: JSON.stringify({need:'weather'})});\n"
                "// paid.status === 200 or 503"
            ),
            "mcp": (
                "POST https://402signal.com/mcp\n"
                '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
                '"params":{"name":"route","arguments":{"need":"weather"}}}\n'
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
Allow: /dashboard
Allow: /pulse
Allow: /health
Allow: /llms.txt
Allow: /robots.txt
Allow: /mcp
Allow: /mcp.json
Allow: /.well-known/mcp.json
Allow: /preview
Allow: /rails

Sitemap: https://402signal.com/
"""

LLMS_TXT = """# 402Signal

> Pay a penny. Get a live payable URL — or an honest miss.

402Signal is a fail-closed x402 router at https://402signal.com
We probe first. Live means an unpaid HTTP 402 with a parseable payment envelope, not merely reachable.
$0.01 = 10000 atomic USDC (6 decimals). Retry unpaid 402 with PAYMENT-SIGNATURE.
HTTP 200 = live URL plus target contract. HTTP 503 = typed miss_reason.

## Paid

- POST /route  $0.01 USDC on Base, Solana, or Algorand
- x402scan skips Algorand/GoPlausible; POST /route returns a currently-alive Algo 402 plus the target contract when that's what is live.
- Body: {"need": "what you want", "url": "https://optional"}
- Agents that intend to pay should POST /route, not GET.
- GET /route with Accept: application/json (or no Accept) returns HTTP 402 so crawlers can index payment. Browsers that send Accept: text/html get a human page.
- Unpaid → HTTP 402 (amount 10000 atomic = $0.01, 6 decimals)
- Paid live hit → HTTP 200 + URL that 402s with a payment envelope + target {method,inputSchema,outputSchema,accepts,facilitator,amountAtomic,displayAmount,timeoutSeconds}
- If inputSchema is missing: live may be true, invocable false, miss_reason no_input_schema
- Paid miss → HTTP 503 {live:false, miss_reason}
- miss_reason enum: no_candidates, no_402_envelope, reachable_200, probe_timeout, quote_expired, invalid_need, upstream_5xx, ssrf, no_input_schema
- POST /mcp tools/call name=route is the same paid probe (unpaid tools/call also 402s)
- MCP bazaar type is mcp, toolName is route. Live MCP: https://402signal.com/mcp and /mcp.json

## Public

- GET /  human homepage
- GET /dashboard  sample lookups per chain (Base / Solana / Algorand)
- GET /pulse  same snapshot as JSON, including samples[]
- GET /preview?need=weather  cached hits + prices + freshness + not_probed:true (does not probe, does not charge)
- GET /rails  three pay-in networks, asset, amountAtomic, facilitators, feePayers, maxTimeoutSeconds, per-rail up+latency
- GET /health  {"ok":true}
- GET /openapi.json
- GET /.well-known/x402
- GET /.well-known/x402.json
- GET /mcp.json
- GET /mcp  same as /mcp.json
- GET /.well-known/mcp.json
- GET /llms.txt
- GET /robots.txt
- POST /mcp initialize, tools/list, and tools/call preview (no payment)

## Listed on

- Glama: https://glama.ai/mcp/servers/402signal/402signal
- MCP Registry: https://registry.modelcontextprotocol.io/?q=402signal (io.github.402signal/402signal)
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

Unpaid 402 → sign accepts[0] → PAYMENT-SIGNATURE → 200 or 503.

curl:
  curl -sS -D - https://402signal.com/route -H 'Content-Type: application/json' -d '{"need":"weather"}'
  curl -sS https://402signal.com/route -H 'Content-Type: application/json' -H "PAYMENT-SIGNATURE: $SIG" -d '{"need":"weather"}'

Fetch:
  const r = await fetch("https://402signal.com/route", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({need:"weather"})});
  const paid = await fetch("https://402signal.com/route", {method:"POST", headers:{"Content-Type":"application/json","PAYMENT-SIGNATURE": sig}, body: JSON.stringify({need:"weather"})});

MCP tools/call:
  POST https://402signal.com/mcp
  {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"route","arguments":{"need":"weather"}}}
  unpaid HTTP 402; retry the same body with PAYMENT-SIGNATURE.

Wallet checklist: USDC 6 decimals; include extra.feePayer on Solana/Algorand; POST-not-GET; v1 network is base, v2 accepts[].network is CAIP-2 eip155:8453. Copy the target facilitator from accepts[].extra.facilitator. Do not default to x402.org.

USDC has 6 decimals. Protocol amount 10000 is one cent, not 10,000 dollars.
"""
