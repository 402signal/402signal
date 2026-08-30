"""Stdlib JSON-RPC MCP over HTTP. Paid tool: route. Cached preflight: preview."""

from __future__ import annotations

from live402 import payment, pulse
from live402.route import handle_route

ROUTE_DESCRIPTION = (
    "Pay $0.01 USDC for a live payable URL or an honest miss. Unpaid tools/call "
    "returns HTTP 402. Retry with PAYMENT-SIGNATURE."
)

PREVIEW_DESCRIPTION = (
    "Cached catalog preflight from /pulse. Returns hits, prices, freshness, "
    "not_probed:true. Does not probe and does not charge. Pay POST /route or "
    "tools/call route for a fail-closed live probe."
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
            ],
        },
        "tried": {"type": "integer"},
        "latency_ms": {"type": ["integer", "null"]},
        "schema_source": {"type": ["string", "null"], "enum": ["envelope", "catalog", "bazaar", None]},
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
        "hits": {"type": "array"},
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
        if name != "route":
            return 200, jsonrpc_error(req_id, -32601, "Unknown tool"), None
        return handle_route(args, headers, resource_url, bazaar=payment.BAZAAR_MCP)
    return 200, jsonrpc_error(req_id, -32601, "Method not found"), None
