"""HTTP 402 payload + payment header parse. No payment keys stored."""

from __future__ import annotations

import base64
import json
import os

DEFAULT_PAYTO = "0xb18fc2275f36dae99eb215caeff03b431f887d16"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# Ross's Algorand receive address (no key stored here).
DEFAULT_PAYTO_ALGORAND = "N2JSJZCSORMYGYO2NSIYRUEMBFRHEOMYODVXV2MXYYHB5H2JVUGG6NJ4NQ"
USDC_ALGORAND_ASA = "31566704"  # USDC on Algorand mainnet
ALGORAND_MAINNET = "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="
ALGORAND_FACILITATOR = "https://facilitator.goplausible.xyz"
ALGORAND_FEE_PAYER = "ZMFK2OI7ZBD2U27ISERZC4S6LKM6WMFJPZQ4MYNJDZ2VNBNMBA67RA22AA"
DEFAULT_PAYTO_SOLANA = "HCM423cyKYVUoq9GvmqUphZwYVB6M2wez34i9jzSewLy"
USDC_SOLANA_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_MAINNET = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SOLANA_FACILITATOR = "https://facilitator.payai.network"
SOLANA_FEE_PAYER = "CjNFTjvBhbJJd2B5ePPMHRLx1ELZpa8dwQgGL727eKww"
BASE_CAIP2 = "eip155:8453"
CDP_FACILITATOR = "https://api.cdp.coinbase.com/platform/v2/x402"
# $0.01 USDC, 6 decimals
AMOUNT_ATOMIC = "10000"
AMOUNT_USD = "$0.01"

# Spec-shaped bazaar declaration for POST /route.
# See https://github.com/x402-foundation/x402/blob/main/specs/extensions/bazaar.md
BAZAAR_EXTENSION = {
    "info": {
        "input": {
            "type": "http",
            "method": "POST",
            "bodyType": "json",
            "body": {
                "need": "erc20 token balance",
                "url": "https://example.com/x402/balance",
            },
        },
        "output": {
            "type": "json",
            "example": {
                "live": True,
                "url": "https://example.com/x402/balance",
                "status": 402,
                "latency_ms": 87,
                "has_402_challenge": True,
                "probed_at": "2026-08-29T22:00:00-04:00",
                "health": {
                    "live": True,
                    "last_probe": "2026-08-29T22:00:00-04:00",
                    "latency_ms": 87,
                    "has_402_challenge": True,
                    "status": 402,
                },
            },
        },
    },
    "schema": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "input": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "const": "http"},
                    "method": {"type": "string", "enum": ["POST", "PUT", "PATCH"]},
                    "bodyType": {"type": "string", "enum": ["json", "form-data", "text"]},
                    "body": {
                        "type": "object",
                        "properties": {
                            "need": {"type": "string"},
                            "url": {"type": "string"},
                        },
                        "required": ["need"],
                    },
                    "queryParams": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "headers": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["type", "method", "bodyType", "body"],
                "additionalProperties": False,
            },
            "output": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "example": {"type": "object"},
                },
                "required": ["type"],
            },
        },
        "required": ["input"],
    },
}


def payto_address() -> str:
    raw = (os.environ.get("PAYTO_ADDRESS") or DEFAULT_PAYTO).strip()
    return raw or DEFAULT_PAYTO


def payto_algorand() -> str:
    raw = (os.environ.get("PAYTO_ALGORAND") or DEFAULT_PAYTO_ALGORAND).strip()
    return raw or DEFAULT_PAYTO_ALGORAND


def payto_solana() -> str:
    raw = (os.environ.get("PAYTO_SOLANA") or DEFAULT_PAYTO_SOLANA).strip()
    return raw or DEFAULT_PAYTO_SOLANA


def payment_presented(headers) -> bool:
    """True if a client sent a payment header. This stub does not verify it."""
    keys = (
        "x-payment",
        "payment-signature",
        "payment-payload",
        "x-payment-signature",
    )
    for key in keys:
        val = headers.get(key)
        if val and str(val).strip():
            return True
    return False


# MCP bazaar so CDP indexes the route tool, not only HTTP POST /route.
# Live MCP: https://402signal.com/mcp and /mcp.json
BAZAAR_MCP = {
    "info": {
        "input": {
            "type": "mcp",
            "toolName": "route",
            "description": (
                "Pay $0.01 USDC (10000 atomic, 6 decimals) for a live payable URL "
                "or an honest miss. Unpaid tools/call returns HTTP 402. Retry with "
                "PAYMENT-SIGNATURE. HTTP 200 = live unpaid 402 envelope plus target "
                "contract. HTTP 503 = miss (miss_reason). POST, not GET."
            ),
            "transport": "streamable-http",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "need": {
                        "type": "string",
                        "description": "What to route (plain English).",
                    },
                    "url": {
                        "type": "string",
                        "description": "Optional https URL to probe.",
                    },
                },
                "required": ["need"],
            },
            "example": {"need": "erc20 token balance"},
        },
        "output": {
            "type": "json",
            "example": {
                "live": True,
                "url": "https://example.com/x402/balance",
                "invocable": True,
                "target": {
                    "method": "POST",
                    "inputSchema": {"type": "object", "properties": {"address": {"type": "string"}}},
                    "outputSchema": {"type": "object"},
                    "accepts": [],
                    "facilitator": "https://api.cdp.coinbase.com/platform/v2/x402",
                    "amountAtomic": "10000",
                    "displayAmount": "$0.01",
                    "timeoutSeconds": 60,
                },
                "miss_reason": None,
                "tried": 1,
                "latency_ms": 87,
            },
        },
    },
    "schema": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "input": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "const": "mcp"},
                    "toolName": {"type": "string"},
                    "description": {"type": "string"},
                    "transport": {"type": "string", "enum": ["streamable-http", "sse"]},
                    "inputSchema": {"type": "object"},
                    "example": {"type": "object"},
                },
                "required": ["type", "toolName", "inputSchema"],
                "additionalProperties": False,
            },
            "output": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "example": {"type": "object"},
                },
                "required": ["type"],
            },
        },
        "required": ["input"],
    },
}



def _algorand_extra(sender: str | None = None) -> dict:
    """Facilitator + feePayer + tag. suggestedParams / unsignedGroup from algo_tx."""
    extra = {
        "name": "USD Coin",
        "facilitator": ALGORAND_FACILITATOR,
        "feePayer": ALGORAND_FEE_PAYER,
        "displayAmount": AMOUNT_USD,
        "tag": "x402-global-challenge",
    }
    try:
        from live402.algo_tx import algorand_accept_extra
        extra.update(
            algorand_accept_extra(
                ALGORAND_FEE_PAYER,
                payto_algorand(),
                USDC_ALGORAND_ASA,
                AMOUNT_ATOMIC,
                sender=sender,
            )
        )
    except Exception:
        try:
            from live402.algod import suggested_params
            params = suggested_params()
            if isinstance(params, dict) and params:
                extra["suggestedParams"] = params
        except Exception:
            pass
    return extra


def payment_required(resource_url: str, bazaar: dict | None = None, algorand_sender: str | None = None) -> dict:
    pay_to = payto_address()
    return {
        "x402Version": 2,
        "error": "Payment required",
        "payTo": pay_to,
        "network": "base",
        "asset": "USDC",
        "amount": AMOUNT_USD,
        "resource": {
            "url": resource_url,
            "description": "Fail-closed live-endpoint x402 route. Pay $0.01 for a live URL or an honest miss.",
            "mimeType": "application/json",
            "serviceName": "402Signal",
            "tags": ["x402", "router", "probe"],
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": BASE_CAIP2,
                "asset": USDC_BASE,
                "currency": USDC_BASE,
                "amount": AMOUNT_ATOMIC,
                "payTo": pay_to,
                "maxTimeoutSeconds": 60,
                "extra": {
                    "name": "USD Coin",
                    "version": "2",
                    "facilitator": CDP_FACILITATOR,
                    "caip2": BASE_CAIP2,
                    "displayAmount": AMOUNT_USD,
                },
            },
            {
                "scheme": "exact",
                "network": SOLANA_MAINNET,
                "asset": USDC_SOLANA_MINT,
                "currency": USDC_SOLANA_MINT,
                "amount": AMOUNT_ATOMIC,
                "payTo": payto_solana(),
                "maxTimeoutSeconds": 60,
                "extra": {
                    "name": "USD Coin",
                    "facilitator": SOLANA_FACILITATOR,
                    "feePayer": SOLANA_FEE_PAYER,
                    "displayAmount": AMOUNT_USD,
                },
            },
            {
                "scheme": "exact",
                "network": ALGORAND_MAINNET,
                "asset": USDC_ALGORAND_ASA,
                "currency": USDC_ALGORAND_ASA,
                "amount": AMOUNT_ATOMIC,
                "payTo": payto_algorand(),
                "maxTimeoutSeconds": 60,
                "extra": _algorand_extra(algorand_sender),
            },
        ],
        "extensions": {"bazaar": bazaar or BAZAAR_EXTENSION},
        "help": {
            "docs": "https://402signal.com/llms.txt",
            "openapi": "https://402signal.com/openapi.json",
            "mcp": "https://402signal.com/mcp.json",
            "dashboard": "https://402signal.com/dashboard",
            "rails": ["base", "solana", "algorand"],
            "amount": "$0.01",
            "contact": "https://x.com/402Signal",
            "post": "POST /route with PAYMENT-SIGNATURE after this 402. Agents should POST, not GET.",
        },
    }


def payment_required_header(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def payment_response_header(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _header_get(headers, *names) -> str:
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if getter:
        for name in names:
            val = getter(name)
            if val and str(val).strip():
                return str(val).strip()
        return ""
    lowered = {str(k).lower(): v for k, v in dict(headers).items()}
    for name in names:
        val = lowered.get(name.lower())
        if val and str(val).strip():
            return str(val).strip()
    return ""


def _decode_payment_blob(raw: str) -> dict | None:
    text = (raw or "").strip()
    if not text:
        return None
    candidates = [text]
    try:
        padded = text + ("=" * ((4 - len(text) % 4) % 4))
        decoded = base64.b64decode(padded, validate=False)
        candidates.insert(0, decoded.decode("utf-8"))
    except Exception:
        pass
    for item in candidates:
        try:
            payload = json.loads(item)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def extract_payment_payload(headers) -> dict | None:
    """Parse PAYMENT-SIGNATURE (v2) or X-PAYMENT (v1/v2). Fail closed on junk."""
    raw = _header_get(
        headers,
        "PAYMENT-SIGNATURE",
        "X-PAYMENT",
        "PAYMENT-PAYLOAD",
        "X-PAYMENT-SIGNATURE",
    )
    if not raw:
        return None
    return _decode_payment_blob(raw)


def _norm(value) -> str:
    return str(value or "").strip().lower()


def rail_of_network(network: str) -> str | None:
    n = _norm(network)
    if not n:
        return None
    if n.startswith("algorand") or "algorand" in n:
        return "algorand"
    if n.startswith("solana") or "solana" in n:
        return "solana"
    if n in {"base", "eip155:8453"} or n.startswith("eip155:8453"):
        return "base"
    return None


def rail_of_accept(accept: dict) -> str:
    return rail_of_network((accept or {}).get("network")) or "base"


def token_of(req: dict) -> str:
    asset = str((req or {}).get("asset") or "").strip()
    currency = str((req or {}).get("currency") or "").strip()
    if asset.upper() in {"USDC", "USD", ""}:
        return currency or asset
    return asset or currency


def _accepted_from_payload(payload: dict) -> dict:
    accepted = payload.get("accepted")
    if isinstance(accepted, dict):
        return accepted
    # x402 v1: scheme/network at top level
    out = {}
    for key in ("scheme", "network", "asset", "payTo", "amount", "extra"):
        if key in payload:
            out[key] = payload[key]
    return out


def match_accept(payload: dict, required: dict) -> dict | None:
    """Pick the advertised accept that matches the client's chosen rail."""
    accepted = _accepted_from_payload(payload or {})
    rail = rail_of_network(accepted.get("network") or payload.get("network") or "")
    if not rail:
        pay_to = _norm(accepted.get("payTo"))
        if pay_to and pay_to == _norm(payto_algorand()):
            rail = "algorand"
        elif pay_to and pay_to == _norm(payto_solana()):
            rail = "solana"
        elif pay_to and pay_to == _norm(payto_address()):
            rail = "base"
    if not rail:
        return None
    client_pay = accepted.get("payTo")
    client_amount = accepted.get("amount")
    client_token = token_of(accepted)
    for item in required.get("accepts") or []:
        if rail_of_network(item.get("network")) != rail:
            continue
        if client_pay and _norm(client_pay) != _norm(item.get("payTo")):
            continue
        if client_amount is not None and str(client_amount) != str(item.get("amount")):
            continue
        our_token = token_of(item)
        if client_token and our_token:
            ct, ot = _norm(client_token), _norm(our_token)
            if ct != ot and ct not in {"usdc", "usd"} and ot not in {"usdc", "usd"}:
                continue
        return item
    return None


def official_requirements(accept: dict) -> dict:
    """Facilitator PaymentRequirements: CAIP-2 network + token address as asset."""
    rail = rail_of_accept(accept)
    extra = dict((accept or {}).get("extra") or {})
    extra.setdefault("name", "USD Coin")
    if rail == "solana":
        extra.setdefault("facilitator", SOLANA_FACILITATOR)
        extra.setdefault("feePayer", SOLANA_FEE_PAYER)
        extra.pop("tag", None)
        return {
            "scheme": accept.get("scheme") or "exact",
            "network": SOLANA_MAINNET,
            "amount": str(accept.get("amount") or AMOUNT_ATOMIC),
            "asset": USDC_SOLANA_MINT,
            "payTo": accept.get("payTo") or payto_solana(),
            "maxTimeoutSeconds": int(accept.get("maxTimeoutSeconds") or 60),
            "extra": extra,
        }
    if rail == "algorand":
        extra.setdefault("facilitator", ALGORAND_FACILITATOR)
        extra.setdefault("feePayer", ALGORAND_FEE_PAYER)
        extra.setdefault("tag", "x402-global-challenge")
        extra.pop("suggestedParams", None)
        extra.pop("unsignedGroup", None)
        extra.pop("decimals", None)
        extra.pop("sender", None)
        return {
            "scheme": accept.get("scheme") or "exact",
            "network": ALGORAND_MAINNET,
            "amount": str(accept.get("amount") or AMOUNT_ATOMIC),
            "asset": USDC_ALGORAND_ASA,
            "payTo": accept.get("payTo") or payto_algorand(),
            "maxTimeoutSeconds": int(accept.get("maxTimeoutSeconds") or 60),
            "extra": extra,
        }
    extra.setdefault("version", "2")
    extra.setdefault("facilitator", CDP_FACILITATOR)
    extra["caip2"] = BASE_CAIP2
    extra.pop("tag", None)
    return {
        "scheme": accept.get("scheme") or "exact",
        "network": BASE_CAIP2,
        "amount": str(accept.get("amount") or AMOUNT_ATOMIC),
        "asset": USDC_BASE,
        "payTo": accept.get("payTo") or payto_address(),
        "maxTimeoutSeconds": int(accept.get("maxTimeoutSeconds") or 60),
        "extra": extra,
    }


def ensure_bazaar(payload: dict) -> dict:
    """Echo bazaar on the payload so facilitators can index the catalog."""
    out = dict(payload or {})
    ext = dict(out.get("extensions") or {})
    existing = ext.get("bazaar")
    if not isinstance(existing, dict) or not existing:
        ext["bazaar"] = BAZAAR_EXTENSION
    else:
        merged = dict(BAZAAR_EXTENSION)
        merged.update(existing)
        if isinstance(existing.get("info"), dict):
            info = dict(BAZAAR_EXTENSION.get("info") or {})
            info.update(existing["info"])
            merged["info"] = info
        ext["bazaar"] = merged
    out["extensions"] = ext
    return out


def normalize_payload_for_facilitator(payload: dict, requirements: dict) -> dict:
    """Keep client payload, overlay official accepted fields, echo bazaar."""
    out = ensure_bazaar(payload or {})
    accepted = dict(out.get("accepted") or _accepted_from_payload(out))
    for key in ("scheme", "network", "amount", "asset", "payTo", "maxTimeoutSeconds"):
        if key in requirements:
            accepted[key] = requirements[key]
    extra = dict(accepted.get("extra") or {})
    extra.update(requirements.get("extra") or {})
    accepted["extra"] = extra
    out["accepted"] = accepted
    out["x402Version"] = out.get("x402Version") or 2
    if "resource" not in out and isinstance(payload, dict):
        pass
    return out
