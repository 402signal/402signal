"""Explicit Algorand network configs for Falcon PQ checkpoint anchors.

Both testnet and mainnet exist in code. This PR's production live path
stays TestNet. MainNet broadcast defaults off. Automatic MainNet submit
is a later GO and is not enabled here.

Submit provider (algod POST) and confirm provider (indexer GET +
decode + semantic verify) are separate allowlists. Confirmation is
never HTTP 200 or a returned txid alone.

Independence is an organization check, not a hostname check.
AlgoNode (*.algonode.cloud) and Nodely (*.nodely.dev / *.nodely.io)
are the same operator. Their public indexers report the same Nodely
build. That pair is not independent confirmation.
AlgoExplorer public indexer hosts return HTML landing pages, not
JSON. Algorand Foundation does not publish a no-key public indexer.
Before MainNet GO, confirmation must use a 402Signal-operated node
or a different-organization public API. Fetch+decode+verify stays
required even after that.
"""

from __future__ import annotations

from dataclasses import dataclass

# Protocol base min (algod min-fee) is 1000 µAlgo today.
# Falcon-1024 adds 2x that base, so the uncongested Falcon floor is 3000.
PROTOCOL_BASE_MIN = 1000
FALCON_EXTRA_MIN_MULT = 2
MIN_FEE = PROTOCOL_BASE_MIN * (1 + FALCON_EXTRA_MIN_MULT)  # 3000
MAX_FEE = 30000
# Official Falcon-1024 sizes (Algorand consensus / docs).
FALCON_F1_PK_LEN = 1793
FALCON_F1_SIG_MAX = 1423

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
MAINNET_CANARY_ENV = "LIVE402_PQ_FALCON_MAINNET_CANARY"
MAINNET_ADDRESS_ENV = "LIVE402_PQ_FALCON_MAINNET_ADDRESS"
MAINNET_SIGNER_TOKEN_ENV = "LIVE402_PQ_SIGNER_MAINNET_TOKEN"
MAINNET_SIGNER_HOST_ENV = "LIVE402_PQ_SIGNER_MAINNET_HOST"
MAINNET_SIGNER_PORT_ENV = "LIVE402_PQ_SIGNER_MAINNET_PORT"
MAINNET_SIGNER_DEFAULT_HOST = "402signal-pq-signer-mainnet.internal"
MAINNET_SIGNER_DEFAULT_PORT = 9091

# Hardcoded organization map. Unknown host => not independent.
ORG_NODELY = "nodely"
ORG_SIGNAL = "402signal"
PROVIDER_ORGS = {
    "testnet-api.algonode.cloud": ORG_NODELY,
    "testnet-idx.algonode.cloud": ORG_NODELY,
    "mainnet-api.algonode.cloud": ORG_NODELY,
    "mainnet-idx.algonode.cloud": ORG_NODELY,
    "mainnet-api.4160.nodely.dev": ORG_NODELY,
    "mainnet-idx.4160.nodely.dev": ORG_NODELY,
    "mainnet-api.4160.nodely.io": ORG_NODELY,
    "mainnet-idx.4160.nodely.io": ORG_NODELY,
    "testnet-api.4160.nodely.dev": ORG_NODELY,
    "testnet-idx.4160.nodely.dev": ORG_NODELY,
}

# Second MainNet confirm hostname (Nodely current public indexer).
# Same organization as AlgoNode. Allowlisted for fetch+decode only.
NODELY_MAINNET_CONFIRM_HOST = "mainnet-idx.4160.nodely.dev"
NODELY_MAINNET_CONFIRM_TXN_URL = "https://mainnet-idx.4160.nodely.dev/v2/transactions/"


@dataclass(frozen=True)
class NetworkConfig:
    """One Algorand network the PQ anchor code may speak.

    name: testnet | mainnet
    genesis_id / genesis_hash: exact consensus identity
    submit_host / submit_url: allowlisted algod POST
    confirm_host / confirm_txn_url / pending_url: fetch+decode only
    explorer_tx_url: public lookup prefix
    address_env: public Falcon f1 address env (never a secret)
    min_fee / max_fee: µAlgo. Falcon required fee is
    max(fee_per_byte * signed_Falcon_txn_size, 3*protocol_base_min).
    Cap is hard. Caller cannot select the fee.
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
# Confirm allowlist is fetch+decode hosts. MainNet pending (submit algod)
# is not treated as independent confirmation.
CONFIRM_HOST_ALLOWLIST = {
    TESTNET_NAME: frozenset({TESTNET.confirm_host, TESTNET.submit_host}),
    MAINNET_NAME: frozenset(
        {MAINNET.confirm_host, NODELY_MAINNET_CONFIRM_HOST}
    ),
}
PENDING_HOST_ALLOWLIST = {
    TESTNET_NAME: frozenset({TESTNET.submit_host}),
    MAINNET_NAME: frozenset({MAINNET.submit_host}),
}

# Default MainNet submit and confirm are both Nodely (AlgoNode brand).
# A second allowlisted confirm hostname exists. Same org. Not independent.
CONFIRM_INDEPENDENT_OF_SUBMIT = False
CONFIRM_INDEPENDENCE_STATUS = "not_met_same_org_nodely"
CONFIRM_INDEPENDENCE_REQUIREMENT = (
    "Before MainNet GO, confirmation must use a 402Signal-operated "
    "Algorand node or a public API from a different organization than "
    "the submit host. AlgoNode and Nodely are the same operator. "
    "AlgoExplorer public indexer hosts return HTML, not JSON. "
    "Fetched txn is still decoded and semantically verified locally."
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


def pending_host_allowlisted(network: str, host: str) -> bool:
    allowed = PENDING_HOST_ALLOWLIST.get((network or "").strip().lower(), frozenset())
    return (host or "").strip().lower() in allowed


def provider_org(host: str) -> str:
    """Hardcoded organization for an allowlisted host. Empty if unknown."""
    return PROVIDER_ORGS.get((host or "").strip().lower(), "")


def confirmation_independent(submit_host: str, confirm_host: str) -> bool:
    """True only when both hosts map to known, different organizations."""
    submit_org = provider_org(submit_host)
    confirm_org = provider_org(confirm_host)
    if not submit_org or not confirm_org:
        return False
    return submit_org != confirm_org


def mainnet_confirmation_policy(cfg: NetworkConfig | None = None) -> dict:
    """Descriptor fields for trust v2 and monitor. No secrets."""
    net = cfg or MAINNET
    submit_org = provider_org(net.submit_host)
    confirm_org = provider_org(net.confirm_host)
    independent = confirmation_independent(net.submit_host, net.confirm_host)
    return {
        "require": "fetch_and_decode_actual_txn",
        "http_200_or_txid_not_sufficient": True,
        "public_status_from": "confirmed_only",
        "independent_provider": independent,
        "same_trust_domain_not_sufficient": True,
        "submit_host": net.submit_host,
        "confirm_host": net.confirm_host,
        "submit_org": submit_org,
        "confirm_org": confirm_org,
        "second_confirm_host_allowlisted": NODELY_MAINNET_CONFIRM_HOST,
        "second_confirm_org": provider_org(NODELY_MAINNET_CONFIRM_HOST),
        "algonode_and_nodely_same_org": True,
        "algoexplorer_public_indexer": "retired_html_landing",
        "default_submit_and_confirm": "nodely_same_org",
        "before_mainnet_go": (
            "402Signal-operated node or different-org provider; "
            "fetch+decode+verify still required"
        ),
    }
