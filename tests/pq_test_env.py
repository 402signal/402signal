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
    "LIVE402_PQ_TEST_SUPPORT",
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
    """Drop MainNet/TestNet PQ identity envs.

    Does not arm TEST SUPPORT. Call arm_test_support() when a test
    needs the archived TestNet path.
    """
    for key in MAINNET_ENV_KEYS:
        os.environ.pop(key, None)


def arm_test_support() -> None:
    """TEST SUPPORT only. Explicit TestNet identity for tests/archive."""
    os.environ["LIVE402_PQ_TEST_SUPPORT"] = "1"
    os.environ.setdefault("LIVE402_PQ_FALCON_NETWORK", "testnet")
    os.environ.setdefault("LIVE402_PQ_LOG_EPOCH", "testnet-v1")


def falcon_f1_fixture_pk(tag: bytes = b"") -> bytes:
    """Sanitized Falcon-1024 pk shape (1793 bytes). Not a live key."""
    from live402.pq import algo_anchor

    seed = bytes(tag) + bytes((0x80 + (i % 0x40)) for i in range(algo_anchor.FALCON_F1_PK_LEN))
    return seed[: algo_anchor.FALCON_F1_PK_LEN]


def falcon_f1_fixture_sig(tag: bytes = b"") -> bytes:
    """Sanitized Falcon-1024 compressed-sig shape (1233 bytes). Not a live sig."""
    from live402.pq import algo_anchor

    seed = bytes(tag) + bytes((0xC0 + (i % 0x20)) for i in range(algo_anchor.FALCON_F1_SIG_LIVE))
    return seed[: algo_anchor.FALCON_F1_SIG_LIVE]


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
