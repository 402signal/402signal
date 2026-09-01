"""Explicit Algorand network configs for Falcon PQ checkpoint anchors.

Both testnet and mainnet exist in code. This PR's production live path
stays TestNet. MainNet broadcast defaults off. Automatic MainNet submit
is a later GO and is not enabled here.

Submit provider (algod POST) and confirm provider (indexer/algod GET +
decode) are separable. Confirmation is never HTTP 200 or a returned txid
alone.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_FEE = 3000
MAX_FEE = 30000

TESTNET_NAME = "testnet"
MAINNET_NAME = "mainnet"
TESTNET_GENESIS_ID = "testnet-v1.0"
MAINNET_GENESIS_ID = "mainnet-v1.0"
TESTNET_GENESIS_HASH = "SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI="
MAINNET_GENESIS_HASH = "wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="

NETWORK_ENV = "LIVE402_PQ_FALCON_NETWORK"
BROADCAST_ENV = "LIVE402_PQ_FALCON_BROADCAST"
ADDRESS_ENV = "LIVE402_PQ_FALCON_ADDRESS"
MAINNET_BROADCAST_ENV = "LIVE402_PQ_FALCON_MAINNET_BROADCAST"
MAINNET_ADDRESS_ENV = "LIVE402_PQ_FALCON_MAINNET_ADDRESS"
MAINNET_SIGNER_TOKEN_ENV = "LIVE402_PQ_SIGNER_MAINNET_TOKEN"
MAINNET_SIGNER_HOST_ENV = "LIVE402_PQ_SIGNER_MAINNET_HOST"
MAINNET_SIGNER_PORT_ENV = "LIVE402_PQ_SIGNER_MAINNET_PORT"
MAINNET_SIGNER_DEFAULT_HOST = "402signal-pq-signer-mainnet.internal"
MAINNET_SIGNER_DEFAULT_PORT = 9091


@dataclass(frozen=True)
class NetworkConfig:
    """One Algorand network the PQ anchor code may speak.

    name: testnet | mainnet
    genesis_id / genesis_hash: exact consensus identity
    submit_host / submit_url: allowlisted algod POST
    confirm_host / confirm_txn_url / pending_url: fetch+decode only
    explorer_tx_url: public lookup prefix
    address_env: public Falcon f1 address env (never a secret)
    min_fee / max_fee: µAlgo. Required fee is current; cap is hard.
    broadcast_env: distinct per network. TestNet flag never sends MainNet.
    """

    name: str
    genesis_id: str
    genesis_hash: str
    submit_host: str
    submit_url: str
    confirm_host: str
    confirm_txn_url: str
    pending_url: str
    explorer_tx_url: str
    address_env: str
    min_fee: int
    max_fee: int
    broadcast_env: str


TESTNET = NetworkConfig(
    name=TESTNET_NAME,
    genesis_id=TESTNET_GENESIS_ID,
    genesis_hash=TESTNET_GENESIS_HASH,
    submit_host="testnet-api.algonode.cloud",
    submit_url="https://testnet-api.algonode.cloud/v2/transactions",
    confirm_host="testnet-idx.algonode.cloud",
    confirm_txn_url="https://testnet-idx.algonode.cloud/v2/transactions/",
    pending_url="https://testnet-api.algonode.cloud/v2/transactions/pending/",
    explorer_tx_url="https://testnet.explorer.perawallet.app/tx/",
    address_env=ADDRESS_ENV,
    min_fee=MIN_FEE,
    max_fee=MAX_FEE,
    broadcast_env=BROADCAST_ENV,
)

MAINNET = NetworkConfig(
    name=MAINNET_NAME,
    genesis_id=MAINNET_GENESIS_ID,
    genesis_hash=MAINNET_GENESIS_HASH,
    submit_host="mainnet-api.algonode.cloud",
    submit_url="https://mainnet-api.algonode.cloud/v2/transactions",
    confirm_host="mainnet-idx.algonode.cloud",
    confirm_txn_url="https://mainnet-idx.algonode.cloud/v2/transactions/",
    pending_url="https://mainnet-api.algonode.cloud/v2/transactions/pending/",
    explorer_tx_url="https://explorer.perawallet.app/tx/",
    address_env=MAINNET_ADDRESS_ENV,
    min_fee=MIN_FEE,
    max_fee=MAX_FEE,
    broadcast_env=MAINNET_BROADCAST_ENV,
)

NETWORKS = {TESTNET_NAME: TESTNET, MAINNET_NAME: MAINNET}

SUBMIT_HOST_ALLOWLIST = {
    TESTNET_NAME: frozenset({TESTNET.submit_host}),
    MAINNET_NAME: frozenset({MAINNET.submit_host}),
}
CONFIRM_HOST_ALLOWLIST = {
    TESTNET_NAME: frozenset({TESTNET.confirm_host, TESTNET.submit_host}),
    MAINNET_NAME: frozenset({MAINNET.confirm_host, MAINNET.submit_host}),
}

# Default URLs above are both AlgoNode. That is a separable config, not
# independent confirmation. Same trust domain is not sufficient.
CONFIRM_INDEPENDENT_OF_SUBMIT = False
CONFIRM_INDEPENDENCE_STATUS = "not_met_algonode_same_trust_domain"
CONFIRM_INDEPENDENCE_REQUIREMENT = (
    "Before MainNet GO, confirmation must use a genuinely independent "
    "provider or a 402Signal-operated Algorand node. Fetched txn is still "
    "decoded and semantically verified locally. AlgoNode submit + AlgoNode "
    "confirm does not satisfy independent confirmation."
)


class UnknownNetwork(ValueError):
    """Fail closed: name is not an explicit configured network."""


def get_network(name: str) -> NetworkConfig:
    key = (name or "").strip().lower()
    cfg = NETWORKS.get(key)
    if cfg is None:
        raise UnknownNetwork("unknown network")
    return cfg


def network_for_genesis_id(genesis_id: str) -> NetworkConfig | None:
    gen = (genesis_id or "").strip()
    for cfg in NETWORKS.values():
        if cfg.genesis_id == gen:
            return cfg
    return None


def submit_host_allowlisted(network: str, host: str) -> bool:
    allowed = SUBMIT_HOST_ALLOWLIST.get((network or "").strip().lower(), frozenset())
    return (host or "").strip().lower() in allowed


def confirm_host_allowlisted(network: str, host: str) -> bool:
    allowed = CONFIRM_HOST_ALLOWLIST.get((network or "").strip().lower(), frozenset())
    return (host or "").strip().lower() in allowed
