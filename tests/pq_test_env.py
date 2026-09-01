"""Scoped cleanup for PQ / MainNet env vars. No secrets are valued here."""

from __future__ import annotations

import os

MAINNET_ENV_KEYS = (
    "LIVE402_PQ_FALCON_NETWORK",
    "LIVE402_PQ_FALCON_BROADCAST",
    "LIVE402_PQ_FALCON_MAINNET_BROADCAST",
    "LIVE402_PQ_FALCON_MAINNET_CANARY",
    "LIVE402_PQ_FALCON_ADDRESS",
    "LIVE402_PQ_FALCON_MAINNET_ADDRESS",
    "LIVE402_PQ_SIGNER_TOKEN",
    "LIVE402_PQ_SIGNER_MAINNET_TOKEN",
    "LIVE402_PQ_SIGNER_MAINNET_HOST",
    "LIVE402_PQ_SIGNER_MAINNET_PORT",
    "LIVE402_PQ_LOG_EPOCH",
    "LIVE402_PQ_LOG_ORIGIN",
    "LIVE402_PQ_LOG_SK",
    "LIVE402_PQ_LOG_SK_MAINNET",
    "LIVE402_PQ_LOG_VKEY",
    "LIVE402_PQ_LOG_VKEY_MAINNET",
    "LIVE402_PQ_LOG_DB",
    "LIVE402_PQ_CONFIRM_HOST",
    "LIVE402_PQ_CONFIRM_TXN_URL",
    "LIVE402_PQ_CONFIRM_PROVIDER",
    "LIVE402_PQ_CONFIRM_TATUM_API_KEY",
    "LIVE402_PQ_CONFIRM_NOWNODES_API_KEY",
    "LIVE402_PQ_CONFIRM_INDEXER_TOKEN",
    "CONFIRM_MAINNET_CANARY",
)


def clear_pq_env() -> None:
    """Drop MainNet/TestNet PQ identity envs so later tests stay TestNet."""
    for key in MAINNET_ENV_KEYS:
        os.environ.pop(key, None)


def insert_authorized_fixture(
    *,
    tree_size: int,
    origin: str,
    root,
    checkpoint: str,
    signed: bytes,
    send_state: str,
    request_id: str = "fixture",
    at: int = 1,
    submitted: bool = False,
    txid: str = "",
    expected_txid: str = "",
    fee_policy: str = "",
    fv: int = 0,
    lv: int = 0,
    send_attempted_at: int = 0,
) -> dict:
    """Direct SQL insert for tests. Bypasses production monotonicity.

    Production code must use store.save_authorized_checkpoint. This
    helper exists so tests can seed SEND_ATTEMPTED/SUBMITTED rows
    without weakening those invariants.
    """
    import json

    from live402.pq import store

    root_hex = bytes(root).hex() if isinstance(root, (bytes, bytearray)) else str(root or "")
    policy = fee_policy if isinstance(fee_policy, str) else json.dumps(fee_policy or {})
    with store._lock:
        conn = store._connect()
        conn.execute(
            "INSERT OR REPLACE INTO authorized_anchors"
            "(tree_size, origin, root, checkpoint, request_id, signed, at, submitted, txid, "
            "send_state, expected_txid, fee_policy, fv, lv, send_attempted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(tree_size),
                origin or "",
                root_hex,
                checkpoint or "",
                request_id,
                bytes(signed or b""),
                int(at),
                1 if submitted else 0,
                txid or "",
                send_state,
                expected_txid or "",
                policy,
                int(fv or 0),
                int(lv or 0),
                int(send_attempted_at or 0),
            ),
        )
        conn.commit()
    return store.authorized_at(int(tree_size)) or {}
