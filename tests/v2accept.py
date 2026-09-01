"""Complete v2 observed accepts for unit tests. Production validation stays strict."""

from __future__ import annotations

from live402 import payment


def v2_accept(network, asset, amount, pay_to, **extra):
    amt = amount if isinstance(amount, str) else str(amount)
    acc = {
        "scheme": "exact",
        "network": network,
        "asset": asset,
        "amount": amt,
        "payTo": pay_to,
        "maxTimeoutSeconds": 60,
    }
    acc.update(extra)
    return acc


def v2_envelope(accepts):
    return {"x402Version": 2, "accepts": list(accepts)}


def attach_v2(row, accepts=None):
    accs = accepts if accepts is not None else row.get("accepts")
    if not isinstance(accs, list):
        return row
    completed = []
    for raw in accs:
        if not isinstance(raw, dict):
            continue
        network = raw.get("network") or payment.BASE_CAIP2
        rail = payment.rail_of_network(network) or row.get("rail") or "base"
        asset = raw.get("asset") or payment.usdc_asset_for_rail(rail) or payment.USDC_BASE
        amount = raw.get("amount")
        if amount is None:
            amount = row.get("amount") or "10000"
        pay_to = raw.get("payTo") or row.get("payTo")
        completed.append(v2_accept(network, asset, amount, pay_to))
    row["accepts"] = completed
    row["envelope"] = v2_envelope(completed)
    return row
