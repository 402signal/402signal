"""Stdlib JSON-RPC MCP over HTTP. Paid tool: route. Cached preflight: preview."""

from __future__ import annotations

from live402 import payment, pulse, validate
from live402.route import handle_route

ROUTE_DESCRIPTION = (
    "Pay $0.01 USDC for a live payable URL or an honest miss. Unpaid tools/call "
    "returns HTTP 402. Retry with PAYMENT-SIGNATURE."
)

PREVIEW_DESCRIPTION = (
    "Request-time catalog preflight. Returns discovery_matches, displayed hits, "
    "seller claims vs 402Signal observations, not_probed:true. Does not probe "
    "and does not charge. Pay POST /route or tools/call route for a live probe."
)

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "need": {"type": "string", "description": "What to route (plain English)."},
        "url": {"type": "string", "description": "Optional https URL to probe."},
        "prefer_network": {
            "type": "string",
            "enum": ["base", "solana", "algorand"],
            "description": "Prefer this pay-in rail when ranking catalog hits.",
        },
        "objective": {
            "type": "string",
            "enum": ["best", "cheapest", "fastest", "most_reliable"],
            "description": "Best-of-N ranking among live probes. Unknown values fall back to best.",
        },
        "max_amount_atomic": {
            "type": "integer",
            "minimum": 0,
            "description": "Drop live hits whose known atomic amount exceeds this bound.",
        },
        "max_latency_ms": {
            "type": "integer",
            "minimum": 0,
            "description": "Drop live hits whose known latency exceeds this bound.",
        },
        "require_invocable": {
            "type": "boolean",
            "description": "If true, drop live hits without an input schema.",
        },
        "networks": {
            "type": "array",
            "items": {"type": "string", "enum": ["base", "solana", "algorand"]},
            "description": "Restrict selectable hits to these pay-in rails.",
        },
    },
    "required": ["need"],
}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "live": {"type": "boolean"},
        "url": {"type": ["string", "null"]},
        "invocable": {"type": "boolean"},
        "target": {
            "type": ["object", "null"],
            "properties": {
                "method": {"type": "string"},
                "inputSchema": {"type": ["object", "null"]},
                "outputSchema": {"type": ["object", "null"]},
                "accepts": {"type": "array"},
                "facilitator": {"type": ["string", "null"]},
                "amountAtomic": {"type": ["string", "null"]},
                "displayAmount": {"type": ["string", "null"]},
                "timeoutSeconds": {"type": "integer"},
            },
        },
        "miss_reason": {
            "type": ["string", "null"],
            "enum": [
                "no_candidates",
                "no_402_envelope",
                "no_payto",
                "reachable_200",
                "probe_timeout",
                "quote_expired",
                "invalid_need",
                "upstream_5xx",
                "ssrf",
                "no_input_schema",
                "constraints_unmet",
                "probe_budget_exhausted",
            ],
        },
        "tried": {"type": "integer"},
        "discovery_matches": {"type": "integer"},
        "candidates_considered": {"type": "integer"},
        "candidates_probed": {"type": "integer"},
        "probe_budget_exhausted": {"type": "boolean"},
        "latency_ms": {"type": ["integer", "null"]},
        "schema_source": {"type": ["string", "null"], "enum": ["envelope", "catalog", "bazaar", None]},
        "objective": {"type": "string", "enum": ["best", "cheapest", "fastest", "most_reliable"]},
        "compared": {"type": "array"},
    },
}

PREVIEW_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "need": {"type": "string", "description": "What to look up in the cache."},
        "prefer_network": {
            "type": "string",
            "enum": ["base", "solana", "algorand"],
            "description": "Prefer this pay-in rail when ranking cached hits.",
        },
    },
    "required": ["need"],
}

PREVIEW_OUTPUT_SCHEMA = {
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
        "hits": {"type": "array"},
        "miss_reason": {"type": ["string", "null"]},
    },
}

VALIDATE_DESCRIPTION = (
    "Unpaid probe: is this seller URL agent-ready? Fail-closed SSRF. "
    "Returns readiness, claimed vs observed, flags. Does not charge."
)

VALIDATE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "https URL of the seller endpoint to probe."},
    },
    "required": ["url"],
}

VALIDATE_OUTPUT_SCHEMA = {
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
        "miss_reason": {"type": ["string", "null"]},
    },
}

TOOLS = [
    {
        "name": "route",
        "description": ROUTE_DESCRIPTION,
        "inputSchema": INPUT_SCHEMA,
        "outputSchema": OUTPUT_SCHEMA,
    },
    {
        "name": "preview",
        "description": PREVIEW_DESCRIPTION,
        "inputSchema": PREVIEW_INPUT_SCHEMA,
        "outputSchema": PREVIEW_OUTPUT_SCHEMA,
    },
    {
        "name": "validate",
        "description": VALIDATE_DESCRIPTION,
        "inputSchema": VALIDATE_INPUT_SCHEMA,
        "outputSchema": VALIDATE_OUTPUT_SCHEMA,
    },
]


def manifest() -> dict:
    return {
        "name": "402Signal",
        "version": "0.4.0",
        "description": ROUTE_DESCRIPTION,
        "tools": TOOLS,
    }


def jsonrpc_initialize(req_id) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "402Signal", "version": "0.4.0"},
        },
    }


def jsonrpc_tools_list(req_id) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}


def jsonrpc_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def is_paid_call(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("method") != "tools/call":
        return False
    params = payload.get("params") or {}
    return isinstance(params, dict) and params.get("name") == "route"


def is_preview_call(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("method") != "tools/call":
        return False
    params = payload.get("params") or {}
    return isinstance(params, dict) and params.get("name") == "preview"


def is_validate_call(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("method") != "tools/call":
        return False
    params = payload.get("params") or {}
    return isinstance(params, dict) and params.get("name") == "validate"


def _preview_result(args: dict) -> dict:
    need = ""
    if isinstance(args, dict) and isinstance(args.get("need"), str):
        need = args.get("need") or ""
    prefer = args.get("prefer_network") if isinstance(args, dict) else None
    return pulse.preview_need(need, prefer_network=prefer)


def handle_mcp(payload: dict, headers, resource_url: str) -> tuple[int, dict, dict | None]:
    """JSON-RPC 2.0. initialize, tools/list, preview are unpaid. tools/call route is x402-gated."""
    if not isinstance(payload, dict):
        return 400, {"error": "JSON object required"}, None
    method = payload.get("method")
    req_id = payload.get("id")
    if method == "initialize":
        return 200, jsonrpc_initialize(req_id), None
    if method == "notifications/initialized":
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": {}}, None
    if method == "tools/list":
        return 200, jsonrpc_tools_list(req_id), None
    if method == "tools/call":
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        name = (params or {}).get("name")
        args = (params or {}).get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return 400, {"error": "arguments must be an object"}, None
        if name == "preview":
            return 200, _preview_result(args), None
        if name == "validate":
            url = args.get("url") if isinstance(args, dict) else ""
            _code, body = validate.validate_url(url if isinstance(url, str) else "")
            return 200 if _code != 400 else 400, body, None
        if name != "route":
            return 200, jsonrpc_error(req_id, -32601, "Unknown tool"), None
        return handle_route(args, headers, resource_url, bazaar=payment.BAZAAR_MCP)
    return 200, jsonrpc_error(req_id, -32601, "Method not found"), None
