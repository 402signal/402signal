"""Operator snapshot of PQ log and Falcon anchor state.

Public status still comes from CONFIRMED only. This snapshot is for
operators and tests. It never includes private keys, mnemonics, HMAC
tokens, or Fly secrets. Balance is not fetched in this PR.
"""

from __future__ import annotations

from live402.pq import EPOCH_TESTNET, MMD_SECONDS, ORIGIN
from live402.pq import algo_anchor
from live402.pq import log_identity
from live402.pq import store
from live402.pq import trust
from live402.pq import worker
from live402.pq.signer_client import token_configured


def snapshot() -> dict:
    """Monitoring fields. No secrets. Public status remains CONFIRMED-only."""
    auth = worker.last_authorized()
    conf = worker.last_confirmed()
    tree_size = int(store.size() or 0)
    auth_size = int(auth.get("size") or 0)
    conf_size = int(conf.get("size") or 0)
    submitted = bool(auth.get("submitted"))
    submitted_txid = str(auth.get("txid") or "") if submitted else ""
    epoch = log_identity.configured_epoch()
    network = algo_anchor.configured_network() or algo_anchor.TESTNET_NAME
    try:
        submit = algo_anchor.submit_provider(
            network if network in {algo_anchor.TESTNET_NAME, algo_anchor.MAINNET_NAME} else algo_anchor.TESTNET_NAME
        )
        confirm = algo_anchor.confirm_provider(
            network if network in {algo_anchor.TESTNET_NAME, algo_anchor.MAINNET_NAME} else algo_anchor.TESTNET_NAME
        )
    except Exception:
        submit = {"network": algo_anchor.TESTNET_NAME, "kind": "submit", "host": "", "url": ""}
        confirm = {"network": algo_anchor.TESTNET_NAME, "kind": "confirm", "host": "", "url": ""}
    return {
        "epoch": epoch,
        "origin": store.origin() or ORIGIN,
        "tree_size": tree_size,
        "root": store.root(tree_size).hex() if tree_size >= 0 else "",
        "last_authorized": {
            "size": auth_size,
            "at": int(auth.get("at") or 0),
            "request_id": str(auth.get("request_id") or ""),
        },
        "last_submitted": {
            "size": auth_size if submitted else 0,
            "txid": submitted_txid,
            "submitted": submitted,
        },
        "last_confirmed": {
            "size": conf_size,
            "at": int(conf.get("at") or 0),
            "txid": str(conf.get("txid") or ""),
            "round": int(conf.get("round") or 0),
        },
        "gaps": {
            "authorized_ahead_of_confirmed": max(0, auth_size - conf_size),
            "tree_ahead_of_confirmed": max(0, tree_size - conf_size),
            "submitted_unconfirmed": bool(submitted and submitted_txid and str(conf.get("txid") or "") != submitted_txid),
        },
        "errors": {
            "integrity": bool(tree_size < conf_size),
            "mainnet_broadcast_set": algo_anchor.mainnet_broadcast_requested(),
            "automatic_mainnet": algo_anchor.automatic_mainnet_enabled(),
        },
        "signer": {
            "testnet_token_configured": token_configured(),
            "mainnet_token_configured": algo_anchor.mainnet_signer_configured(),
            "reads_broadcast": False,
            "broadcasts": False,
        },
        "confirm_provider": {
            "host": confirm.get("host") or "",
            "url": confirm.get("url") or "",
            "kind": "fetch_decode_verify",
            "independent_of_submit": bool(confirm.get("independent_of_submit")),
            "independence_status": confirm.get("independence_status") or "",
        },
        "submit_provider": {
            "host": submit.get("host") or "",
            "url": submit.get("url") or "",
        },
        "balance": {
            "fetched": False,
            "microalgo": None,
        },
        "fee": {
            "max_fee": algo_anchor.MAX_FEE,
            "min_fee": algo_anchor.MIN_FEE,
            "protocol_base_min": algo_anchor.PROTOCOL_BASE_MIN,
            "falcon_min": algo_anchor.MIN_FEE,
            "formula": "max(fee_per_byte * signed_falcon_size, 3 * protocol_base_min)",
            "cap_policy": "fail_closed_if_required_exceeds_max",
        },
        "broadcast": {
            "testnet_flag": algo_anchor.broadcast_requested(),
            "mainnet_flag": algo_anchor.mainnet_broadcast_requested(),
            "automatic_mainnet": False,
            "network": network,
        },
        "trust": {
            "live_version": int((trust.trust_root() or {}).get("version") or 1),
            "v2_prepared": True,
            "not_mainnet_go": True,
            "mmd_seconds": MMD_SECONDS,
            "default_epoch": EPOCH_TESTNET,
        },
    }


ALERTS = (
    {
        "id": "confirmed_behind_sla",
        "when": "tree has grown for 15 minutes or 1000 leaves without a new CONFIRMED anchor",
    },
    {
        "id": "authorized_unconfirmed_gap",
        "when": "last_authorized.size > last_confirmed.size and submitted txid is missing or stale",
    },
    {
        "id": "local_log_inconsistent",
        "when": "store.size < last_confirmed.size (GET /ready fails)",
    },
    {
        "id": "signer_unavailable",
        "when": "LIVE402_PQ_SIGNER_TOKEN unset or 6PN dial fails while a checkpoint is due",
    },
    {
        "id": "confirm_provider_error",
        "when": "fetch+decode of a submitted txid fails or verify_fetched_anchor rejects",
    },
    {
        "id": "fee_at_or_over_cap",
        "when": "required fee > MAX_FEE=30000 (fail closed; do not raise the cap)",
    },
    {
        "id": "low_falcon_balance",
        "when": "Falcon account ALGO is below the documented fee buffer (see docs/pq-funding.md)",
    },
    {
        "id": "unexpected_non_pq1_txn",
        "when": "any txn on the Falcon account that is not the PQ1 pay-0 self-Falcon f1 construction",
        "severity": "incident",
    },
    {
        "id": "mainnet_flag_set",
        "when": "LIVE402_PQ_FALCON_MAINNET_BROADCAST is 1 before the later GO",
        "severity": "incident",
    },
)
