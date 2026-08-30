"""Stdlib JSON-RPC MCP over HTTP. One paid tool: route."""

from __future__ import annotations

from live402.route import handle_route

ROUTE_DESCRIPTION = (
    "Fail-closed live-endpoint router; unpaid HTTP POST /route returns 402; "
    "this MCP tools/call without payment also 402s."
)

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "need": {"type": "string", "description": "What to route (plain English)."},
        "url": {"type": "string", "description": "Optional https URL to probe."},
    },
}

TOOLS = [
    {
        "name": "route",
        "description": ROUTE_DESCRIPTION,
        "inputSchema": INPUT_SCHEMA,
    }
]


def manifest() -> dict:
    return {
        "name": "402Signal",
        "version": "0.3.0",
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
            "serverInfo": {"name": "402Signal", "version": "0.3.0"},
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


def handle_mcp(payload: dict, headers, resource_url: str) -> tuple[int, dict, dict | None]:
    """JSON-RPC 2.0. initialize and tools/list are free. tools/call route is x402-gated."""
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
        if name != "route":
            return 200, jsonrpc_error(req_id, -32601, "Unknown tool"), None
        args = (params or {}).get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return 400, {"error": "arguments must be an object"}, None
        return handle_route(args, headers, resource_url)
    return 200, jsonrpc_error(req_id, -32601, "Method not found"), None
