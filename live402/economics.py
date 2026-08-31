"""Rail economics with mandatory provenance. Same model for Base, Solana, Algorand.

No hidden rail preference. Algorand may win only when a documented or
observed figure supports it. Fees without a USD conversion stay unknown.
"""

from __future__ import annotations

from live402 import payment

PROVENANCE_OBSERVED = "402signal_observed"
PROVENANCE_REFERENCE = "protocol_reference"
PROVENANCE_UNKNOWN = "unknown"

# Confirmed 2026-08-31 against primary sources. Do not invent a USD fee.
#
# Algorand average block time 2.82s and instant block-level finality:
#   https://dev.algorand.co/concepts/transactions/blocks/
#   "Algorand confirms blocks every 2.82 seconds on average."
#   "Algorand achieves instant finality at the block level."
# Algorand min fee is 1,000 microAlgo, not USD (no FX in this process):
#   https://dev.algorand.co/concepts/transactions/fees/
#
# Base L2 block inclusion ~2s for ordinary L2 payments (not L1 withdrawals):
#   https://docs.base.org/base-chain/network-information/transaction-finality
#   "L2 Block Inclusion: ~2s"
# Base L1 batch finality is a later stage (~20m) and is not used here.
# Base gas is variable; no protocol USD fee.
#
# Solana official RPC docs define `finalized` commitment but do not publish
# a current stable wall-clock. Slot time is in flux (SIMD-0525). Unknown.
RAIL_FINALITY = {
    "base": {
        "value_ms": 2000,
        "stage": "l2_block_inclusion",
        "provenance": PROVENANCE_REFERENCE,
        "citation": "https://docs.base.org/base-chain/network-information/transaction-finality",
        "note": "Official Base docs: L2 block inclusion ~2s for ordinary L2 payments. Not L1 batch finality (~20m) and not a withdrawal.",
    },
    "algorand": {
        "value_ms": 2820,
        "stage": "block_certified",
        "provenance": PROVENANCE_REFERENCE,
        "citation": "https://dev.algorand.co/concepts/transactions/blocks/",
        "note": "Official Algorand docs: average block time 2.82s; instant finality at block (no forks).",
    },
    "solana": {
        "value_ms": None,
        "stage": None,
        "provenance": PROVENANCE_UNKNOWN,
        "citation": "https://solana.com/docs/rpc",
        "note": "Official Solana RPC docs define finalized commitment but no current stable wall-clock. Slot time is changing. Not invented.",
    },
}

RAIL_CHAIN_FEE_USD = {
    "base": {
        "value": None,
        "provenance": PROVENANCE_UNKNOWN,
        "reason": "variable_gas_no_usd_oracle",
        "citation": None,
    },
    "solana": {
        "value": None,
        "provenance": PROVENANCE_UNKNOWN,
        "reason": "variable_priority_fee_no_usd_oracle",
        "citation": None,
    },
    "algorand": {
        "value": None,
        "provenance": PROVENANCE_UNKNOWN,
        "reason": "min_fee_is_microalgo_not_usd",
        "native": {
            "value": 1000,
            "unit": "microAlgo",
            "provenance": PROVENANCE_REFERENCE,
            "citation": "https://dev.algorand.co/concepts/transactions/fees/",
            "note": "Official min fee 1,000 microAlgo (0.001 Algo) when uncongested. Not converted to USD.",
        },
        "citation": "https://dev.algorand.co/concepts/transactions/fees/",
    },
}


def _as_float(val):
    if val is None or val == "" or isinstance(val, bool):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _field(value, provenance: str, **extra) -> dict:
    row = {"value": value, "provenance": provenance}
    row.update(extra)
    return row


def _unknown(reason: str, **extra) -> dict:
    return _field(None, PROVENANCE_UNKNOWN, reason=reason, **extra)


def _rail_of(opt, result=None) -> str | None:
    if isinstance(opt, dict) and opt.get("rail") in payment.SUPPORTED_RAILS:
        return opt["rail"]
    if isinstance(result, dict) and result.get("rail") in payment.SUPPORTED_RAILS:
        return result["rail"]
    if isinstance(opt, dict):
        return payment._rail_name(opt.get("network") or opt.get("rail"))
    return None


def for_option(opt, result=None) -> dict:
    """Economics for one CURRENT observed payment option. Same keys on every rail."""
    rail = _rail_of(opt, result)
    merchant = None
    merchant_prov = PROVENANCE_UNKNOWN
    if isinstance(opt, dict) and opt.get("normalized_usd") is not None:
        merchant = _as_float(opt.get("normalized_usd"))
        merchant_prov = PROVENANCE_OBSERVED
    elif isinstance(result, dict):
        usd = None
        for candidate in payment.payment_options_from_result(result):
            if candidate.get("normalized_usd") is not None:
                usd = _as_float(candidate.get("normalized_usd"))
                break
        if usd is not None:
            merchant = usd
            merchant_prov = PROVENANCE_OBSERVED

    fee_spec = RAIL_CHAIN_FEE_USD.get(rail or "") or {
        "value": None,
        "provenance": PROVENANCE_UNKNOWN,
        "reason": "unknown_rail",
    }
    chain_fee = _field(
        fee_spec.get("value"),
        fee_spec.get("provenance") or PROVENANCE_UNKNOWN,
        reason=fee_spec.get("reason"),
        citation=fee_spec.get("citation"),
    )
    if fee_spec.get("native"):
        chain_fee["native"] = dict(fee_spec["native"])

    facilitator_fee = _unknown("facilitator_fee_not_documented")

    total = None
    total_prov = PROVENANCE_UNKNOWN
    total_reason = "unknown_fee"
    if (
        merchant is not None
        and chain_fee.get("value") is not None
        and facilitator_fee.get("value") is not None
    ):
        total = float(merchant) + float(chain_fee["value"]) + float(facilitator_fee["value"])
        total_prov = PROVENANCE_OBSERVED
        total_reason = None
    total_cost = _field(total, total_prov, reason=total_reason)
    if total is None:
        total_cost["note"] = (
            "Merchant price is not total cost. Unknown fee fails closed for "
            "lowest_total_cost and max_total_cost_usd."
        )

    fin = RAIL_FINALITY.get(rail or "") or {
        "value_ms": None,
        "stage": None,
        "provenance": PROVENANCE_UNKNOWN,
        "citation": None,
        "note": "unknown rail",
    }
    finality = _field(
        fin.get("value_ms"),
        fin.get("provenance") or PROVENANCE_UNKNOWN,
        stage=fin.get("stage"),
        citation=fin.get("citation"),
        note=fin.get("note"),
    )
    # We do not have a settlement ledger. Do not copy probe RTT here.
    settlement = _unknown(
        "no_settlement_ledger",
        note="Not probe RTT and not service p50. Observed settlement is unknown.",
    )
    settlement_or_finality = settlement["value"]
    settlement_or_finality_prov = PROVENANCE_UNKNOWN
    if settlement["value"] is not None:
        settlement_or_finality = settlement["value"]
        settlement_or_finality_prov = settlement["provenance"]
    elif finality.get("value") is not None:
        settlement_or_finality = finality["value"]
        settlement_or_finality_prov = finality["provenance"]

    return {
        "rail": rail,
        "merchant_price_usd": _field(merchant, merchant_prov if merchant is not None else PROVENANCE_UNKNOWN),
        "chain_fee_usd": chain_fee,
        "facilitator_fee_usd": facilitator_fee,
        "total_cost_usd": total_cost,
        "settlement_latency_ms": settlement,
        "finality_ms": finality,
        "settlement_or_finality_ms": _field(
            settlement_or_finality,
            settlement_or_finality_prov,
            note="Observed settlement if present, else protocol_reference finality. Never probe RTT.",
        ),
        "fee_predictability": _unknown("not_measured"),
        "settlement_success": _unknown("no_settlement_ledger"),
        "facilitator_performance": _unknown("not_mixed_with_rails_ping"),
    }


def for_result(result, selected=None) -> dict | None:
    opt = selected if isinstance(selected, dict) else None
    if opt is None and isinstance(result, dict):
        opt = result.get("selected_payment") if isinstance(result.get("selected_payment"), dict) else None
    if opt is None:
        opts = payment.payment_options_from_result(result)
        opt = opts[0] if opts else {"rail": (result or {}).get("rail")}
    return for_option(opt, result)


def total_cost_usd(result, selected=None):
    eco = for_result(result, selected)
    if not eco:
        return None
    field = eco.get("total_cost_usd") or {}
    return field.get("value")


def settlement_or_finality_ms(result, selected=None):
    """Settlement/finality only. Never probe RTT."""
    eco = for_result(result, selected)
    if not eco:
        return None
    field = eco.get("settlement_or_finality_ms") or {}
    val = field.get("value")
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def attach_to_payment(opt, result=None) -> dict | None:
    if not isinstance(opt, dict):
        return None
    out = dict(opt)
    out["economics"] = for_option(opt, result)
    return out
