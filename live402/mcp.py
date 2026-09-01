"""Stdlib JSON-RPC MCP over HTTP. Paid tool: route. Cached preflight: preview."""

from __future__ import annotations

from live402 import payment, pulse, schema_fields, validate
from live402.route import handle_route

ROUTE_DESCRIPTION = payment.CATALOG_DESCRIPTION

PREVIEW_DESCRIPTION = (
    "Request-time catalog preflight over upstream catalogs plus a local shadow. "
    "Returns discovery_matches, displayed hits, claimed vs observed, not_probed:true. "
    "Does not probe and does not charge. Pay tools/call route for a live probe."
)

INPUT_SCHEMA = schema_fields.route_body_schema()

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "live": {"type": "boolean"},
        "url": {"type": ["string", "null"]},
        "challenge_observed": {"type": "boolean"},
        "payable": {"type": "boolean"},
        "invocable": {"type": "boolean"},
        "selected_payment": {
            "type": ["object", "null"],
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
        "changes": {
            "type": "object",
            "properties": {
                "payTo_changed_at": {"type": ["string", "integer", "null"]},
                "price_changed_at": {"type": ["string", "integer", "null"]},
                "schema_changed_at": {"type": ["string", "integer", "null"]},
            },
        },
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
        "miss_reason": schema_fields.miss_reason_schema(),
        "tried": {"type": "integer"},
        "discovery_matches": {"type": "integer"},
        "candidates_discovered": {"type": "integer"},
        "candidates_considered": {"type": "integer"},
        "candidates_probed": {"type": "integer"},
        "probe_ceiling": {"type": "integer"},
        "probe_budget_exhausted": {"type": "boolean"},
        "candidate_evaluation_complete": {"type": "boolean"},
        "interpreted_constraints": {"type": "object"},
        "unresolved_constraints": {"type": "array"},
        "stop_reason": {
            "type": "string",
            "enum": list(schema_fields.STOP_REASONS),
        },
        "latency_ms": {"type": ["integer", "null"]},
        "schema_source": {"type": ["string", "null"], "enum": ["envelope", "catalog", "bazaar", None]},
        "reputation": {"type": "object"},
        "objective": {
            "type": "string",
            "enum": list(schema_fields.OBJECTIVES),
        },
        "pq_trust": {
            "type": "object",
            "properties": {
                "transparency": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": list(schema_fields.TRANSPARENCY_STATUSES)},
                        "state": {"type": "string", "enum": list(schema_fields.TRANSPARENCY_STATES)},
                        "log_origin": {"type": "string"},
                        "leaf_type": {"type": "string"},
                        "index": {"type": "integer"},
                        "checkpoint_size": {"type": "integer"},
                        "receipt": {"type": "object"},
                        "reveal": {"type": "object"},
                    },
                }
            },
        },
        "compared": {"type": "array"},
    },
}

PREVIEW_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "need": {"type": "string", "description": "What to look up in the cache."},
        "prefer_network": {
            "type": "string",
            "enum": list(schema_fields.RAILS),
            "description": schema_fields.PREFER_NETWORK_DESC,
        },
        "networks": {
            "type": "array",
            "items": {"type": "string", "enum": list(schema_fields.RAILS)},
            "description": "Restrict searchable rails to this set. Unlike prefer_network, other rails are not queried.",
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
        "discovery_via": {"type": "object"},
        "discovery_exhaustive": {"type": "boolean"},
        "hits": {"type": "array"},
        "miss_reason": schema_fields.miss_reason_schema(),
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
        "miss_reason": schema_fields.miss_reason_schema(),
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
        "version": "0.5.0",
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
            "serverInfo": {"name": "402Signal", "version": "0.5.0"},
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
    networks = args.get("networks") if isinstance(args, dict) else None
    return pulse.preview_need(need, prefer_network=prefer, networks=networks)


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
