#!/usr/bin/env python3
"""Reproduce public conformance fixtures with a PUBLIC TEST KEY. Never live I/O."""

import base64
import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from live402 import payment
from live402 import route_binding as rb
from live402.pq import receipt, store


def generate():
    cases = []
    historical = []
    request = {"need": "weather", "max_price_usd": 0.02, "require_route_binding": True}
    # Public deterministic TEST key, unrelated to production log or Falcon keys.
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    with (
        tempfile.TemporaryDirectory() as tmp,
        patch.dict(
            os.environ,
            {
                "LIVE402_FIXTURE": "1",
                "LIVE402_PQ_LOG": "1",
                "LIVE402_ROUTE_BINDING_TTL_S": "60",
                "LIVE402_PQ_LOG_DB": tmp + "/log.sqlite",
            },
        ),
    ):
        store.reset()
        vkey = receipt.configure_signer(key)
        try:
            for i, rail in enumerate(("base", "solana", "algorand", "base")):
                method, request_bytes = ("POST", b"{}") if i == 3 else ("GET", b"")
                acc = next(
                    a
                    for a in payment.payment_required("https://example.com/api")[
                        "accepts"
                    ]
                    if payment.rail_of_accept(a) == rail
                )
                acc = {k: v for k, v in acc.items() if k != "extra"}
                acc["amount"] = "10000"  # Seller price is independent of routing fee.
                env = {"x402Version": 2, "accepts": [acc]}
                if i == 3:
                    # UTF-16 key order, escaped strings and extra data participate
                    # in the cross-language hash; none are silently discarded.
                    env["extensions"] = {
                        "bazaar": {"\U0001f600": "a\nb", "\ue000": "\u2028"}
                    }
                selected = payment.selected_payment_fields(
                    payment.validate_observed_accept(acc, env)
                )
                result = {
                    "url": "https://example.com/api",
                    "live": True,
                    "payable": True,
                    "invocable": True,
                    "status": 402,
                    "payTo": acc["payTo"],
                    "envelope": env,
                    "selected_payment": selected,
                    "probed_at": "2026-09-05T00:00:00Z",
                    "latency_ms": 12,
                    "applied_constraints": {"max_price_usd": 0.02},
                    "binding_observation": {
                        "request": rb.request_context(
                            "https://example.com/api", method, request_bytes
                        ),
                        "observed_at": 1788566400,
                        "quote_sha256": rb.digest(env),
                    },
                }
                result["decision_binding"] = rb.build(result, request, now=1788566401)
                with (
                    patch(
                        "live402.pq.route_v4.secrets.token_bytes",
                        return_value=bytes([i]) * 32,
                    ),
                    patch(
                        "live402.pq.route_v4.secrets.token_hex",
                        return_value=f"{i + 1:064x}",
                    ),
                    patch(
                        "live402.pq.jcs.utc_minutes_z",
                        return_value="2026-09-05T00:00:00Z",
                    ),
                ):
                    result = receipt.attach_to_route(result, request)
                result.pop("binding_observation")
                rb.verify_route(
                    result,
                    request,
                    vkey=vkey,
                    status=402,
                    envelope=env,
                    url=result["url"],
                    method=method,
                    body=request_bytes,
                    now=1788566402,
                )
                cases.append(
                    {
                        "rail": rail,
                        "request": request,
                        "response": result,
                        "challenge": env,
                        "now": 1788566402,
                        "method": method,
                        "body": request_bytes.decode(),
                    }
                )
                if i == 2:
                    # Non-last leaves under a later odd-sized checkpoint catch
                    # proof-order bugs hidden by one-leaf/last-leaf tests.
                    for old in cases[:2]:
                        case = copy.deepcopy(old)
                        proof = case["response"]["pq_trust"]["transparency"]["receipt"]
                        proof["checkpoint"] = result["pq_trust"]["transparency"][
                            "receipt"
                        ]["checkpoint"]
                        proof["inclusion_path"] = [
                            base64.b64encode(p).decode()
                            for p in store.inclusion_path(proof["index"], 3)
                        ]
                        rb.verify_route(
                            case["response"],
                            request,
                            vkey=vkey,
                            status=402,
                            envelope=case["challenge"],
                            url=case["response"]["url"],
                            method="GET",
                            now=case["now"],
                        )
                        historical.append(case)
        finally:
            receipt.configure_signer(None)
            store.reset()
    return {
        "format": "proof_carrying_route_v1_conformance",
        "test_only": True,
        "trusted_vkey": vkey,
        "cases": cases,
        "historical_inclusions": historical,
    }


if __name__ == "__main__":
    out = Path(__file__).resolve().parents[1] / "tests/fixtures/route-binding-v1.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(generate(), indent=2, ensure_ascii=False) + "\n")
    print(out)
