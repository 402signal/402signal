"""HTTP 402 payload + payment header parse. No payment keys stored."""

from __future__ import annotations

import base64
import json
import os
import re

DEFAULT_PAYTO = "0xb18fc2275f36dae99eb215caeff03b431f887d16"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
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
# $0.003 USDC, 6 decimals
AMOUNT_ATOMIC = "3000"
AMOUNT_USD = "$0.003"
ROUTING_BILLING_MODEL = "success_only_v1"
ROUTING_SETTLEMENT_CONDITION = "live_eligible_route_found"
USDC_DECIMALS = 6

# CDP / Bazaar / PayAI / GoPlausible listing blurb. Keep at or under 500 chars.
CATALOG_DESCRIPTION = (
    "402Signal is the independent check an AI agent makes right before spending. "
    "It finds the strongest x402 route across Base, Solana, and Algorand, verifies it live, "
    "and shows the evidence behind the choice. Your agent keeps the wallet. "
    "The routing authorization settles only when a live eligible route is returned. "
    "The decision history is committed to an append-only PQ Trust log. "
    "Production identity is Algorand MainNet."
)

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
                "billing": {
                    "model": ROUTING_BILLING_MODEL,
                    "condition": ROUTING_SETTLEMENT_CONDITION,
                    "asset": "USDC",
                    "amount_atomic": AMOUNT_ATOMIC,
                    "display_amount": AMOUNT_USD,
                    "rail": "base",
                    "settlement_attempted": True,
                    "settled": True,
                    "settlement_state": "settled",
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
                        "anyOf": [{"required": ["need"]}, {"required": ["url"]}],
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
            "description": CATALOG_DESCRIPTION,
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
                    "prefer_network": {
                        "type": "string",
                        "enum": ["base", "solana", "algorand"],
                        "description": "Prefer this pay-in rail when ranking. Searches all supported rails; does not restrict to this rail. Use networks to restrict.",
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
                    "accept_payTo_change": {
                        "type": "boolean",
                        "description": "If true, allow selecting a destination whose payTo just changed for the first time.",
                    },
                    "require_transparency": {
                        "type": "boolean",
                        "description": (
                            "If true, a settled /route winner fails when a signed checkpoint receipt "
                            "cannot be produced. This guarantees delivery, not server-side "
                            "recovery. Securely retain the complete paid /route response, "
                            "especially pq_trust.transparency.receipt and "
                            "pq_trust.transparency.reveal. 402Signal does not retain the "
                            "private reveal and cannot recover it if lost. Default false "
                            "(SEC-ROUTER-004 / A-14): "
                            "a settled winner does not require a durable signed leaf; "
                            "free typed misses create no route-decision leaf."
                        ),
                    },
                },
                "anyOf": [{"required": ["need"]}, {"required": ["url"]}],
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
                "billing": {
                    "model": ROUTING_BILLING_MODEL,
                    "condition": ROUTING_SETTLEMENT_CONDITION,
                    "asset": "USDC",
                    "amount_atomic": AMOUNT_ATOMIC,
                    "display_amount": AMOUNT_USD,
                    "rail": "base",
                    "settlement_attempted": True,
                    "settled": True,
                    "settlement_state": "settled",
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
        "billing": {
            "model": ROUTING_BILLING_MODEL,
            "condition": ROUTING_SETTLEMENT_CONDITION,
            "asset": "USDC",
            "amount_atomic": AMOUNT_ATOMIC,
            "display_amount": AMOUNT_USD,
            "typed_misses_settled": False,
            "seller_payment_separate": True,
        },
        "resource": {
            "url": resource_url,
            "description": CATALOG_DESCRIPTION,
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
            "amount": AMOUNT_USD,
            "billingModel": ROUTING_BILLING_MODEL,
            "settlementCondition": ROUTING_SETTLEMENT_CONDITION,
            "typedMissesSettled": False,
            "sellerPaymentSeparate": True,
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


def _base58_decoded_length(text: str) -> int | None:
    if type(text) is not str or not text or not text.isascii():
        return None
    value = 0
    for char in text:
        idx = _B58_ALPHABET.find(char)
        if idx < 0:
            return None
        value = value * 58 + idx
    pad = len(text) - len(text.lstrip("1"))
    raw_len = 0 if value == 0 else (value.bit_length() + 7) // 8
    return pad + raw_len


def _settlement_txid_ok(value, rail: str) -> bool:
    if type(value) is not str or not value or len(value) > 128:
        return False
    if rail == "base":
        return bool(re.fullmatch(r"0x[0-9a-fA-F]{64}", value))
    if rail == "solana":
        return _base58_decoded_length(value) == 64
    if rail == "algorand":
        if not re.fullmatch(r"[A-Z2-7]{52}", value):
            return False
        try:
            return len(base64.b32decode(value + "====", casefold=False)) == 32
        except Exception:
            return False
    return False


def sanitize_settlement_receipt(payload, rail: str | None = None) -> dict | None:
    """Allowlist one protocol settlement receipt; never reflect raw facilitator data."""
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None
    network = payload.get("network")
    if type(network) is not str or len(network) > 128:
        return None
    expected_networks = {
        "base": BASE_CAIP2,
        "solana": SOLANA_MAINNET,
        "algorand": ALGORAND_MAINNET,
    }
    inferred = rail_of_network(network)
    expected_rail = rail if rail in SUPPORTED_RAILS else inferred
    if (
        expected_rail not in SUPPORTED_RAILS
        or inferred != expected_rail
        or network != expected_networks.get(expected_rail)
    ):
        return None
    transaction = payload.get("transaction")
    if not _settlement_txid_ok(transaction, expected_rail):
        return None
    out = {"success": True, "transaction": transaction, "network": network}
    payer = payload.get("payer")
    if payer is not None:
        if type(payer) is not str or len(payer) > 128:
            return None
        if not valid_payto_for_rail(payer, expected_rail):
            return None
        out["payer"] = payer
    return out


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
    from live402.http_body import reject_json_constant

    for item in candidates:
        try:
            payload = json.loads(item, parse_constant=reject_json_constant)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
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


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(val):
    if val is None or val == "" or isinstance(val, bool):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def rail_of_network(network: str) -> str | None:
    """Internal rail aliases (our 402, prefer_network, match_accept). Case-folded exact ids."""
    n = _norm(network)
    if not n:
        return None
    if n == "base" or n == _norm(BASE_CAIP2):
        return "base"
    if n == "solana" or n == _norm(SOLANA_MAINNET):
        return "solana"
    if n == "algorand" or n == _norm(ALGORAND_MAINNET):
        return "algorand"
    return None


def rail_of_observed_network(network, version: int) -> str | None:
    """Seller HTTP 402 validation. Exact CAIP/network ids. No case-fold, no aliases on v2."""
    if type(network) is not str:
        return None
    if version == 2:
        if network == BASE_CAIP2:
            return "base"
        if network == SOLANA_MAINNET:
            return "solana"
        if network == ALGORAND_MAINNET:
            return "algorand"
        return None
    if version == 1:
        if network == "base" or network == BASE_CAIP2:
            return "base"
        if network == "solana" or network == SOLANA_MAINNET:
            return "solana"
        if network == "algorand" or network == ALGORAND_MAINNET:
            return "algorand"
        return None
    return None


def rail_of_accept(accept: dict) -> str:
    return rail_of_network((accept or {}).get("network")) or "base"


def _rail_name(rail) -> str | None:
    """Short rail name from a short name or CAIP-2 network."""
    if rail is None or rail == "":
        return None
    text = str(rail).strip()
    if not text:
        return None
    low = text.lower()
    if low in {"base", "solana", "algorand", "evm"}:
        return "base" if low == "evm" else low
    return rail_of_network(text)


def infer_rail_from_payto(addr) -> str | None:
    """Guess a rail from address shape. Algorand is 58 chars; EVM is 0x+40 hex."""
    text = _text(addr)
    if not text:
        return None
    if text.startswith("0x") and len(text) == 42:
        try:
            int(text[2:], 16)
        except ValueError:
            return None
        return "base"
    if len(text) == 58 and text.isalnum():
        return "algorand"
    if text.isalnum() and 32 <= len(text) <= 44:
        return "solana"
    return None


def payto_canonical(addr, rail=None) -> str | None:
    """Canonical payTo for a rail. Algorand is uppercase; Base/EVM is lowercase hex."""
    text = _text(addr)
    if not text:
        return None
    r = _rail_name(rail)
    if r == "algorand":
        return text.upper()
    if r == "base":
        return text.lower()
    return text


def payto_equal(a, b, rail=None) -> bool:
    """Rail-aware payTo equality. No universal .lower().

    Base/EVM: case-insensitive hex. Solana: case-sensitive base58.
    Algorand: case-insensitive; canonical form in this repo is uppercase
    (DEFAULT_PAYTO_ALGORAND) and algo_tx.decode_address uses base32.
    """
    left, right = _text(a), _text(b)
    if not left or not right:
        return False
    r = _rail_name(rail)
    if not r:
        ra, rb = infer_rail_from_payto(left), infer_rail_from_payto(right)
        if ra and ra == rb:
            r = ra
        else:
            return left == right
    if r == "solana":
        return left == right
    if r == "algorand":
        return left.upper() == right.upper()
    if r == "base":
        return left.lower() == right.lower()
    return left == right


def _token_equal(a, b, rail) -> bool:
    left, right = _text(a), _text(b)
    if not left or not right:
        return False
    r = _rail_name(rail)
    if r == "solana":
        return left == right
    if r == "algorand":
        return left.upper() == right.upper()
    if r == "base":
        return left.lower() == right.lower()
    return left == right


def known_usdc_asset(asset, network=None) -> bool:
    """True only for the three exact USDC ids. Bare USDC/USD is not those ids."""
    raw = _text(asset)
    if not raw:
        return False
    _ = network
    if _token_equal(raw, USDC_BASE, "base"):
        return True
    if _token_equal(raw, USDC_SOLANA_MINT, "solana"):
        return True
    if _token_equal(raw, USDC_ALGORAND_ASA, "algorand"):
        return True
    return False


def usdc_asset_for_rail(rail) -> str | None:
    r = _rail_name(rail)
    if r == "solana":
        return USDC_SOLANA_MINT
    if r == "algorand":
        return USDC_ALGORAND_ASA
    if r == "base":
        return USDC_BASE
    return None


def _format_usd(usd: float) -> str:
    if usd == 0:
        return "$0.00"
    if usd >= 0.01 or usd == 0:
        rounded = round(usd, 2)
        if abs(usd - rounded) < 1e-12:
            return f"${rounded:.2f}"
    text = f"${usd:.6f}".rstrip("0").rstrip(".")
    return text


def usdc_from_atomic(amount) -> tuple[str | None, float | None]:
    """Known USDC 6 decimals only. 10000 → ('$0.01', 0.01). Never invent dollars for other assets."""
    if amount is None or amount == "":
        return None, None
    raw = str(amount).strip()
    if raw.startswith("$"):
        try:
            usd = float(raw[1:].replace(",", ""))
        except ValueError:
            return raw, None
        return _format_usd(usd) if usd >= 0 else raw, usd
    n = _as_int(raw)
    if n is None:
        return None, None
    usd = n / 1_000_000
    if n == 0:
        return "$0.00", 0.0
    if n % 10_000 == 0:
        return f"${usd:.2f}", usd
    return _format_usd(usd), usd


def _accept_asset(accept: dict) -> str | None:
    asset = _text((accept or {}).get("asset"))
    currency = _text((accept or {}).get("currency"))
    if asset and asset.upper() not in {"USDC", "USD"}:
        return asset
    return asset or currency


def payment_option_from_accept(accept, fallback_network=None) -> dict | None:
    """Explicit payment option. USD only when the asset/value relationship is known."""
    if not isinstance(accept, dict):
        return None
    network = accept.get("network") or fallback_network
    network_s = _text(network)
    rail = _rail_name(network_s)
    asset = _accept_asset(accept)
    raw_amt = accept.get("amount")
    if raw_amt is None or raw_amt == "":
        raw_amt = accept.get("maxAmountRequired")
    amount_atomic = _as_int(raw_amt)
    extra = accept.get("extra") if isinstance(accept.get("extra"), dict) else {}
    seller_display = extra.get("displayAmount")
    if seller_display is not None:
        seller_display = str(seller_display).strip() or None
    if raw_amt is not None and str(raw_amt).strip().startswith("$") and not seller_display:
        seller_display = str(raw_amt).strip()

    known = known_usdc_asset(asset, network_s)
    decimals = USDC_DECIMALS if known else None
    normalized_usd = None
    display_amount = None
    if known and amount_atomic is not None:
        label, usd = usdc_from_atomic(amount_atomic)
        normalized_usd = usd
        display_amount = label
    elif seller_display:
        display_amount = seller_display
    elif amount_atomic is not None and asset:
        display_amount = "%s %s" % (amount_atomic, asset)
    elif amount_atomic is None and raw_amt is None and not asset and not network_s:
        return None

    fac = extra.get("facilitator")
    fac_url = None
    if isinstance(fac, str) and fac.strip().startswith("https://"):
        fac_url = fac.strip()
    elif isinstance(fac, dict):
        cand = str(fac.get("url") or "").strip()
        if cand.startswith("https://"):
            fac_url = cand

    return {
        "network": network_s,
        "rail": rail,
        "asset": asset,
        "amount_atomic": amount_atomic,
        "decimals": decimals,
        "display_amount": display_amount,
        "normalized_usd": normalized_usd,
        "payTo": _text(accept.get("payTo")),
        "facilitator": fac_url,
        "scheme": _text(accept.get("scheme") or extra.get("scheme")),
        "version": accept.get("x402Version", extra.get("version")),
    }


def payment_options_from_accepts(accepts, fallback_network=None) -> list[dict]:
    out: list[dict] = []
    if not isinstance(accepts, list):
        return out
    for acc in accepts:
        opt = payment_option_from_accept(acc, fallback_network)
        if opt:
            out.append(opt)
    return out


def _option_identity(opt: dict) -> tuple:
    rail = opt.get("rail") or ""
    asset = str(opt.get("asset") or "")
    amt = opt.get("amount_atomic")
    net = str(opt.get("network") or "")
    return (rail, net, asset, amt)


def _dedupe_options(opts: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for opt in opts:
        key = _option_identity(opt)
        if key in seen:
            continue
        seen.add(key)
        out.append(opt)
    return out


def observed_accepts(result) -> list[dict]:
    """CURRENT observed accepts only. Never catalog / claimed / discovery.

    Prefer the live 402 envelope, then target.accepts (envelope-normalized).
    Top-level result.accepts is a test/helper fallback when no envelope exists.
    """
    if not isinstance(result, dict):
        return []
    env = result.get("envelope") if isinstance(result.get("envelope"), dict) else {}
    target = result.get("target") if isinstance(result.get("target"), dict) else {}
    env_acc = [a for a in (env.get("accepts") or []) if isinstance(a, dict)]
    if env_acc:
        return env_acc
    tgt_acc = [a for a in (target.get("accepts") or []) if isinstance(a, dict)]
    if tgt_acc:
        return tgt_acc
    return [a for a in (result.get("accepts") or []) if isinstance(a, dict)]


def _accepts_declared(blob) -> bool:
    return isinstance(blob, dict) and "accepts" in blob


def payment_options_from_result(result) -> list[dict]:
    """Observed payment options only. Catalog claims are never promoted.

    If accepts[] exists (even all-invalid or empty), return only valid entries.
    Never synthesize from top-level fields over an existing accepts list.
    Legacy synthesis only when no accepts list is present.
    """
    if not isinstance(result, dict):
        return []
    target = result.get("target") if isinstance(result.get("target"), dict) else {}
    env = result.get("envelope") if isinstance(result.get("envelope"), dict) else {}
    for blob in (env, target, result):
        if not _accepts_declared(blob):
            continue
        raw = blob.get("accepts")
        if not isinstance(raw, list):
            return []
        opts: list[dict] = []
        for acc in raw:
            opt = validate_observed_accept(acc, env)
            if opt:
                opts.append(opt)
        return _dedupe_options(opts)
    fallback = result.get("network") or result.get("rail")
    extra = {}
    display = target.get("displayAmount") or result.get("displayAmount")
    if display:
        extra["displayAmount"] = display
    amount = result.get("amount")
    if amount is None:
        amount = result.get("amountAtomic") or target.get("amountAtomic")
    synth = validate_observed_accept(
        {
            "network": fallback,
            "asset": result.get("asset") or target.get("asset"),
            "currency": result.get("currency"),
            "amount": amount,
            "payTo": result.get("payTo"),
            "extra": extra,
        },
        env,
    )
    return [synth] if synth else []


SUPPORTED_RAILS = frozenset(("base", "solana", "algorand"))
SUPPORTED_X402_VERSIONS = frozenset((1, 2))
SUPPORTED_SCHEMES = frozenset(("exact",))
# Payment-amount bound only. Not the PQ checkpoint integer range.
MAX_ATOMIC_AMOUNT = (2**63) - 1
MAX_ACCEPT_TIMEOUT_SECONDS = 86400
_BASE_PAYTO_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ALGORAND_PAYTO_RE = re.compile(r"^[A-Z2-7]{58}$")
_SOLANA_PAYTO_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def sane_atomic_amount(val) -> int | None:
    """Nonnegative atomic amount in a sane integer bound. Coercing helper, not wire."""
    if isinstance(val, bool) or val is None or val == "":
        return None
    n = _as_int(val)
    if n is None or n < 0 or n > MAX_ATOMIC_AMOUNT:
        return None
    return n


MAX_ATOMIC_TEXT_LEN = 20
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def canonical_atomic_string(val) -> int | None:
    """x402 wire amount: canonical decimal STRING only. No floats, signs, or bombs."""
    if type(val) is not str:
        return None
    if not val or not val.isascii() or not val.isdigit():
        return None
    if len(val) > MAX_ATOMIC_TEXT_LEN:
        return None
    if len(val) > 1 and val[0] == "0":
        return None
    try:
        n = int(val)
    except (ValueError, OverflowError):
        return None
    if n < 0 or n > MAX_ATOMIC_AMOUNT:
        return None
    return n


def _keccak256(data: bytes) -> bytes | None:
    try:
        from Crypto.Hash import keccak as _keccak

        digest = _keccak.new(digest_bits=256)
        digest.update(data)
        return digest.digest()
    except Exception:
        pass
    try:
        import sha3 as _sha3

        return _sha3.keccak_256(data).digest()
    except Exception:
        return None


def _eip55_ok(addr: str) -> bool:
    """Mixed-case EIP-55 when keccak is available. All-lower / all-upper always OK."""
    body = addr[2:]
    if body == body.lower() or body == body.upper():
        return True
    hashed = _keccak256(body.lower().encode("ascii"))
    if hashed is None:
        return True
    hexhash = hashed.hex()
    for char, nibble in zip(body, hexhash):
        if not char.isalpha():
            continue
        if int(nibble, 16) >= 8:
            if char != char.upper():
                return False
        elif char != char.lower():
            return False
    return True


def _b58decode32(text: str) -> bool:
    """Solana payTo: Base58 decode must be exactly 32 bytes."""
    if type(text) is not str or not text or not text.isascii():
        return False
    value = 0
    for char in text:
        idx = _B58_ALPHABET.find(char)
        if idx < 0:
            return False
        value = value * 58 + idx
    pad = 0
    for char in text:
        if char == "1":
            pad += 1
        else:
            break
    if value == 0:
        raw = b"\x00" * pad
    else:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        raw = (b"\x00" * pad) + raw
    return len(raw) == 32


def valid_payto_for_rail(addr, rail) -> bool:
    """Rail-specific payTo. Base EIP-55-safe, Algorand checksum, Solana 32-byte."""
    text = _text(addr)
    if not text:
        return False
    r = _rail_name(rail)
    if r == "base":
        if not _BASE_PAYTO_RE.match(text):
            return False
        return _eip55_ok(text)
    if r == "algorand":
        try:
            from live402.algo_tx import decode_address

            decode_address(text)
            return True
        except Exception:
            return False
    if r == "solana":
        if text.startswith("0x") or text.startswith("0X"):
            return False
        return _b58decode32(text)
    return False


def valid_asset_for_rail(asset, rail) -> bool:
    text = _text(asset)
    if not text:
        return False
    r = _rail_name(rail)
    if r == "base":
        if text.upper() in {"USDC", "USD"}:
            return True
        return bool(_BASE_PAYTO_RE.match(text))
    if r == "algorand":
        if text.upper() in {"USDC", "USD"}:
            return True
        if text.isdigit() and 1 <= int(text) <= MAX_ATOMIC_AMOUNT:
            return True
        return False
    if r == "solana":
        if text.upper() in {"USDC", "USD"}:
            return True
        if text.startswith("0x") or text.startswith("0X"):
            return False
        return bool(_SOLANA_PAYTO_RE.match(text))
    return False


def _timeout_ok(raw) -> bool:
    if raw is None or raw == "":
        return True
    n = _as_int(raw)
    if n is None or n < 1 or n > MAX_ACCEPT_TIMEOUT_SECONDS:
        return False
    return True


def _x402_version_ok(raw) -> bool:
    if raw is None or raw == "":
        return True
    n = _as_int(raw)
    return n in SUPPORTED_X402_VERSIONS


def _scheme_ok(raw) -> bool:
    text = _text(raw)
    if text is None:
        return True
    return text.lower() in SUPPORTED_SCHEMES


def _literal_x402_version(raw) -> int | None:
    if type(raw) is int and raw in SUPPORTED_X402_VERSIONS:
        return raw
    return None


def _literal_timeout(raw) -> int | None:
    if type(raw) is not int:
        return None
    if raw < 1 or raw > MAX_ACCEPT_TIMEOUT_SECONDS:
        return None
    return raw


def _required_scheme(accept: dict, extra: dict) -> str | None:
    scheme = accept.get("scheme")
    extra_scheme = extra.get("scheme") if isinstance(extra, dict) else None
    if type(scheme) is not str or scheme != "exact":
        return None
    if extra_scheme is not None and extra_scheme != "exact":
        return None
    return scheme


def validate_observed_accept(accept, envelope=None) -> dict | None:
    """Version-aware seller accept. Parseable 402 ≠ selectable payment option."""
    if not isinstance(accept, dict):
        return None
    env = envelope if isinstance(envelope, dict) else {}
    extra = accept.get("extra") if isinstance(accept.get("extra"), dict) else {}
    ver = accept.get("x402Version")
    if ver is None:
        ver = env.get("x402Version")
    version = _literal_x402_version(ver)
    if version is None:
        return None
    if _required_scheme(accept, extra) is None:
        return None
    if _literal_timeout(accept.get("maxTimeoutSeconds")) is None:
        return None
    rail = rail_of_observed_network(accept.get("network"), version)
    if rail is None:
        return None
    if version == 2:
        if "amount" not in accept:
            return None
        amount = canonical_atomic_string(accept.get("amount"))
        if amount is None:
            return None
        other = accept.get("maxAmountRequired")
        if other is not None and other != "":
            if canonical_atomic_string(other) != amount:
                return None
    else:
        if "maxAmountRequired" not in accept:
            return None
        amount = canonical_atomic_string(accept.get("maxAmountRequired"))
        if amount is None:
            return None
        other = accept.get("amount")
        if other is not None and other != "":
            if canonical_atomic_string(other) != amount:
                return None
    if "asset" not in accept or not valid_asset_for_rail(accept.get("asset"), rail):
        return None
    if "payTo" not in accept or not valid_payto_for_rail(accept.get("payTo"), rail):
        return None
    wire = dict(accept)
    if version == 2:
        wire["amount"] = accept.get("amount")
    else:
        wire["amount"] = accept.get("maxAmountRequired")
    opt = payment_option_from_accept(wire)
    if opt is None:
        return None
    opt["amount_atomic"] = amount
    opt["rail"] = rail
    opt["network"] = accept.get("network")
    opt["scheme"] = "exact"
    if not is_complete_payment_option(opt, env):
        return None
    return opt


def is_complete_payment_option(opt, envelope=None) -> bool:
    """True iff the option is payable as observed. Fail closed. Never fill from catalog."""
    if not isinstance(opt, dict):
        return False
    if opt.get("rail") not in SUPPORTED_RAILS:
        return False
    network = _text(opt.get("network"))
    if not network or rail_of_network(network) != opt.get("rail"):
        return False
    amount = sane_atomic_amount(opt.get("amount_atomic"))
    if amount is None:
        return False
    if not valid_asset_for_rail(opt.get("asset"), opt.get("rail")):
        return False
    if not valid_payto_for_rail(opt.get("payTo"), opt.get("rail")):
        return False
    env = envelope if isinstance(envelope, dict) else {}
    if env:
        if "x402Version" in env and not _x402_version_ok(env.get("x402Version")):
            return False
        accepts = env.get("accepts") if isinstance(env.get("accepts"), list) else []
        if any(isinstance(a, dict) and "scheme" in a for a in accepts):
            if not _scheme_ok(opt.get("scheme")) or not _text(opt.get("scheme")):
                return False
    elif opt.get("scheme") is not None and not _scheme_ok(opt.get("scheme")):
        return False
    return True


def selected_payment_fields(opt) -> dict | None:
    """Public selected_payment object. One observed option, no mixed rails."""
    if not isinstance(opt, dict):
        return None
    return {
        "rail": opt.get("rail"),
        "network": opt.get("network"),
        "asset": opt.get("asset"),
        "amount_atomic": opt.get("amount_atomic"),
        "display_amount": opt.get("display_amount"),
        "normalized_usd": opt.get("normalized_usd"),
        "payTo": opt.get("payTo"),
        "facilitator": opt.get("facilitator"),
    }


def selected_payment_matches_current_envelope(selected, result) -> bool:
    """Require selected_payment to equal one valid option in this response's envelope.

    This is the settlement provenance boundary. Catalog, target.accepts, and
    legacy top-level fallbacks are intentionally excluded even though they
    remain useful to non-economic display and compatibility code.
    """
    if not isinstance(selected, dict) or not isinstance(result, dict):
        return False
    env = result.get("envelope")
    if not isinstance(env, dict) or type(env.get("x402Version")) is not int:
        return False
    accepts = env.get("accepts")
    if not isinstance(accepts, list) or not accepts:
        return False
    rail = selected.get("rail")
    network = selected.get("network")
    asset = selected.get("asset")
    amount = sane_atomic_amount(selected.get("amount_atomic"))
    pay_to = selected.get("payTo")
    if rail not in SUPPORTED_RAILS or type(network) is not str or amount is None:
        return False
    selected_asset = asset_identity(
        {"rail": rail, "network": network, "asset": asset}
    )
    if selected_asset is None:
        return False
    for accept in accepts:
        opt = validate_observed_accept(accept, env)
        if opt is None or opt.get("rail") != rail or opt.get("network") != network:
            continue
        if sane_atomic_amount(opt.get("amount_atomic")) != amount:
            continue
        if asset_identity(opt) != selected_asset:
            continue
        if not payto_equal(opt.get("payTo"), pay_to, rail):
            continue
        expected = selected_payment_fields(opt)
        if not isinstance(expected, dict):
            continue
        if any(
            selected.get(key) != expected.get(key)
            for key in (
                "rail",
                "network",
                "asset",
                "amount_atomic",
                "display_amount",
                "normalized_usd",
                "payTo",
                "facilitator",
            )
        ):
            continue
        return True
    return False


def asset_identity(opt: dict | None) -> str | None:
    """Comparable asset key. None means unknown / incomparable."""
    if not isinstance(opt, dict):
        return None
    asset = _text(opt.get("asset"))
    if not asset:
        return None
    rail = opt.get("rail") or _rail_name(opt.get("network"))
    if known_usdc_asset(asset, opt.get("network") or rail):
        known = usdc_asset_for_rail(rail)
        return ("usdc:%s" % (known or asset)).lower()
    r = _rail_name(rail)
    if r == "base":
        return "base:%s" % asset.lower()
    if r == "algorand":
        return "algorand:%s" % asset.upper()
    if r == "solana":
        return "solana:%s" % asset
    return "%s:%s" % (r or "unknown", asset)


def accept_identity(acc: dict) -> tuple:
    """Dedupe key for catalog accepts: rail + asset + payTo + amount."""
    if not isinstance(acc, dict):
        return ("", "", "", "")
    rail = rail_of_network(acc.get("network") or "") or ""
    asset = _accept_asset(acc) or ""
    pay = _text(acc.get("payTo")) or ""
    amt = acc.get("amount")
    if amt is None or amt == "":
        amt = acc.get("maxAmountRequired")
    return (rail, str(asset), pay, str(amt) if amt is not None else "")


def prices_equivalent(left, right) -> bool:
    """True when two payment options are the same price. Incomparable → False."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    lu, ru = left.get("normalized_usd"), right.get("normalized_usd")
    if lu is not None and ru is not None:
        return float(lu) == float(ru)
    la, ra = asset_identity(left), asset_identity(right)
    if not la or not ra or la != ra:
        return False
    aa, ab = left.get("amount_atomic"), right.get("amount_atomic")
    if aa is None or ab is None:
        return False
    return int(aa) == int(ab)


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
    for key in ("scheme", "network", "asset", "payTo", "amount", "maxAmountRequired", "extra"):
        if key in payload:
            out[key] = payload[key]
    return out


def inbound_payload_version(payload: dict) -> int | None:
    """Return one unambiguous literal wire version.

    Do not infer a version from amount-field shape and do not ignore an
    explicitly malformed or conflicting nested version.  The router and the
    facilitator must classify the same payment payload.
    """
    if not isinstance(payload, dict):
        return None
    accepted = _accepted_from_payload(payload)
    seen: list[int] = []
    for obj in (payload, accepted):
        if "x402Version" not in obj:
            continue
        ver = _literal_x402_version(obj.get("x402Version"))
        if ver is None:
            return None
        seen.append(ver)
    if not seen or any(ver != seen[0] for ver in seen[1:]):
        return None
    return seen[0]


def inbound_client_network(payload: dict):
    if not isinstance(payload, dict):
        return None
    accepted = _accepted_from_payload(payload)
    if "network" in accepted:
        return accepted.get("network")
    return payload.get("network")


def inbound_client_amount(accepted: dict, version: int):
    """Canonical atomic int. v1 requires maxAmountRequired; v2 requires amount."""
    if not isinstance(accepted, dict):
        return None
    if version == 1:
        if "maxAmountRequired" not in accepted:
            return None
        amount = canonical_atomic_string(accepted.get("maxAmountRequired"))
        if amount is None:
            return None
        other = accepted.get("amount")
        if other is not None and other != "":
            if canonical_atomic_string(other) != amount:
                return None
        return amount
    if "amount" not in accepted:
        return None
    amount = canonical_atomic_string(accepted.get("amount"))
    if amount is None:
        return None
    other = accepted.get("maxAmountRequired")
    if other is not None and other != "":
        if canonical_atomic_string(other) != amount:
            return None
    return amount


def advertised_accept_amount(item: dict):
    if not isinstance(item, dict):
        return None
    raw = item.get("amount")
    if raw is None or raw == "":
        raw = item.get("maxAmountRequired")
    if type(raw) is not str:
        return None
    return canonical_atomic_string(raw)


_TESTNET_NETWORKS = frozenset(
    {
        "eip155:84532",
        "eip155:11155111",
        "eip155:11155420",
        "base-sepolia",
        "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
        "solana:4uhcVJyU9pJkvQyS88uRDiswHXSCkY3z",
    }
)
_TESTNET_MARKERS = ("sepolia", "testnet", "devnet", "goerli", "holesky")

INBOUND_MISMATCH_ERROR = "Payment does not match an advertised accept"
INBOUND_TESTNET_ERROR = "testnet networks are not accepted"


def looks_like_testnet_network(network) -> bool:
    if type(network) is not str:
        return False
    text = network.strip()
    if not text:
        return False
    if text in _TESTNET_NETWORKS:
        return True
    low = text.lower()
    return any(marker in low for marker in _TESTNET_MARKERS)


def inbound_match_error(payload: dict) -> str:
    """402 error when match_accept fails (SEC-ROUTER-005 / A-07)."""
    if looks_like_testnet_network(inbound_client_network(payload)):
        return INBOUND_TESTNET_ERROR
    return INBOUND_MISMATCH_ERROR


def resource_url_of(obj) -> str | None:
    """resource.url from PaymentRequired, PaymentPayload, or a v1 accept."""
    if isinstance(obj, str):
        text = obj.strip()
        return text or None
    if not isinstance(obj, dict):
        return None
    raw = obj.get("resource")
    if isinstance(raw, str):
        text = raw.strip()
        return text or None
    if isinstance(raw, dict):
        url = raw.get("url")
        if isinstance(url, str):
            text = url.strip()
            return text or None
    return None


def match_accept(payload: dict, required: dict) -> dict | None:
    """Pick the advertised accept that matches the client's rail and resource.

    SEC-ROUTER-002: bind resource.url. Fail closed when the request resource
    is missing or the payment's resource.url does not match it. A payment
    authorized for /route must not match /mcp.

    SEC-ROUTER-005 / A-07: require client network, amount, and payTo. No
    omit-to-first-same-rail. Inbound rail is rail_of_observed_network
    (strict CAIP on v2; exact short aliases or CAIP on v1). Testnet and
    unknown networks fail closed. v1 amounts use maxAmountRequired.
    """
    if not isinstance(required, dict):
        return None
    required_url = resource_url_of(required)
    if not required_url:
        return None
    payload = payload if isinstance(payload, dict) else {}
    accepted = _accepted_from_payload(payload)
    payload_url = resource_url_of(payload)
    accepted_url = resource_url_of(accepted)
    if payload_url and payload_url != required_url:
        return None
    if accepted_url and accepted_url != required_url:
        return None
    claimed = payload_url or accepted_url
    if claimed != required_url:
        return None
    version = inbound_payload_version(payload)
    if version is None:
        return None
    rail = rail_of_observed_network(inbound_client_network(payload), version)
    if not rail:
        return None
    client_pay = accepted.get("payTo")
    if not client_pay:
        return None
    client_amount = inbound_client_amount(accepted, version)
    if client_amount is None:
        return None
    client_token = token_of(accepted)
    if not client_token:
        return None
    client_scheme = accepted.get("scheme")
    if type(client_scheme) is not str or client_scheme != "exact":
        return None
    req_ver = _literal_x402_version(required.get("x402Version")) or 2
    for item in required.get("accepts") or []:
        if not isinstance(item, dict):
            continue
        item_url = resource_url_of(item)
        if item_url and item_url != required_url:
            continue
        item_ver = _literal_x402_version(item.get("x402Version")) or req_ver
        if item.get("scheme") != client_scheme:
            continue
        if rail_of_observed_network(item.get("network"), item_ver) != rail:
            continue
        if not payto_equal(client_pay, item.get("payTo"), rail):
            continue
        item_amount = advertised_accept_amount(item)
        if item_amount is None or item_amount != client_amount:
            continue
        our_token = token_of(item)
        if not our_token:
            continue
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
            # Some v1 clients duplicate requirement fields at top level. Keep
            # those fields for compatibility, but never forward a conflicting
            # value beside the canonical accepted object.
            if key in out:
                out[key] = requirements[key]
    extra = dict(accepted.get("extra") or {})
    extra.update(requirements.get("extra") or {})
    accepted["extra"] = extra
    out["accepted"] = accepted
    out["x402Version"] = out.get("x402Version") or 2
    if "resource" not in out and isinstance(payload, dict):
        pass
    return out
