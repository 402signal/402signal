"""Explicit Algorand network configs for Falcon PQ checkpoint anchors.

PRODUCTION is MainNet-only. Unset or unknown network fails closed.
TEST SUPPORT may still name TestNet constants for tests and archive.
MainNet broadcast and automatic submission both default off.

Submit provider (algod POST) and confirm provider (indexer GET +
decode + semantic verify) are separate allowlists. Confirmation is
never HTTP 200 or a returned txid alone.

Independence is an organization check, not a hostname check.
Excluded as independent (same org / AlgoNode-backed, from public
records, not a legal audit):
  AlgoNode (*.algonode.*)
  Nodely (*.4160.nodely.dev, *.4160.nodely.io, *.nodely.dev/.io)
  Allo explorer (allo.info; AlgoNode-developed, Algorand Foundation)
  Oanor (*.oanor.com; documents live reads from public AlgoNode APIs)
A different Nodely hostname is not a different org.

Production confirm path (option B):
  PRIMARY  tatum     algorand-mainnet-indexer.gateway.tatum.io
  FAILOVER nownodes  algo-index.nownodes.io
Do not add Blockdaemon. Confirm enum is tatum|nownodes only.
Fetch+decode+semantic verify stays required. HTTP 200 or a returned
txid is never confirmation. Submit-host pending is never independent.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

# Protocol base min (algod min-fee) is 1000 µAlgo today.
# Falcon-1024 adds 2x that base, so the uncongested Falcon floor is 3000.
PROTOCOL_BASE_MIN = 1000
FALCON_EXTRA_MIN_MULT = 2
MIN_FEE = PROTOCOL_BASE_MIN * (1 + FALCON_EXTRA_MIN_MULT)  # 3000
MAX_FEE = 30000
# Official Falcon-1024 wire bounds. Algorand publishes a 1423-byte maximum.
# Its Falcon CompressedSignature parser documents fewer than two bytes as
# malformed (header + salt-version cannot be present). This is only a
# structural lower bound; it is not cryptographic verification.
FALCON_F1_PK_LEN = 1793
FALCON_F1_SIG_MAX = 1423
FALCON_F1_SIG_MIN = 2
FALCON_F1_SIG_HEADER = 0xBA
FALCON_F1_SIG_SALT_VERSION = 0

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
MAINNET_AUTO_ENV = "LIVE402_PQ_FALCON_MAINNET_AUTO"
MAINNET_AUTO_KILL_ENV = "LIVE402_PQ_FALCON_MAINNET_AUTO_KILL"
MAINNET_ADDRESS_ENV = "LIVE402_PQ_FALCON_MAINNET_ADDRESS"
MAINNET_SIGNER_TOKEN_ENV = "LIVE402_PQ_SIGNER_MAINNET_TOKEN"
MAINNET_SIGNER_HOST_ENV = "LIVE402_PQ_SIGNER_MAINNET_HOST"
MAINNET_SIGNER_PORT_ENV = "LIVE402_PQ_SIGNER_MAINNET_PORT"
MAINNET_SIGNER_DEFAULT_HOST = "402signal-pq-signer-mainnet.internal"
MAINNET_SIGNER_DEFAULT_PORT = 9091
# Static confirm-provider enum. Env chooses the name only, never
# scheme / hostname / path / query / auth-header-name.
CONFIRM_PROVIDER_ENV = "LIVE402_PQ_CONFIRM_PROVIDER"

# Hardcoded organization map. Unknown host => not independent.
ORG_NODELY = "nodely"
ORG_SIGNAL = "402signal"
ORG_TATUM = "tatum"
ORG_NOWNODES = "nownodes"
# Exact hosts. Suffix rules in provider_org cover *.algonode.*,
# *.4160.nodely.*, Allo, and Oanor so a new Nodely hostname stays
# the same org.
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
    "allo.info": ORG_NODELY,
    "status.allo.info": ORG_NODELY,
    "oanor.com": ORG_NODELY,
    "www.oanor.com": ORG_NODELY,
    "api.oanor.com": ORG_NODELY,
    "algorand-mainnet-indexer.gateway.tatum.io": ORG_TATUM,
    "algo-index.nownodes.io": ORG_NOWNODES,
}

# Second MainNet confirm hostname (Nodely current public indexer).
# Same organization as AlgoNode. Allowlisted for fetch+decode only.
NODELY_MAINNET_CONFIRM_HOST = "mainnet-idx.4160.nodely.dev"
NODELY_MAINNET_CONFIRM_TXN_URL = "https://mainnet-idx.4160.nodely.dev/v2/transactions/"


@dataclass(frozen=True)
class ConfirmProvider:
    """Hardcoded fetch+decode provider. Env selects the name only."""

    name: str
    org: str
    host: str
    path_template: str
    auth_header: str
    secret_env: str


# Option B production confirm path. Env selects the name only.
# PRIMARY tatum; OPTIONAL failover nownodes. Do not add Blockdaemon.
CONFIRM_PROVIDER_PRIMARY = "tatum"
CONFIRM_PROVIDER_FAILOVER = "nownodes"
CONFIRM_PROVIDERS = {
    "tatum": ConfirmProvider(
        name="tatum",
        org=ORG_TATUM,
        host="algorand-mainnet-indexer.gateway.tatum.io",
        path_template="/v2/transactions/{txid}",
        auth_header="x-api-key",
        secret_env="LIVE402_PQ_CONFIRM_TATUM_API_KEY",
    ),
    "nownodes": ConfirmProvider(
        name="nownodes",
        org=ORG_NOWNODES,
        host="algo-index.nownodes.io",
        path_template="/v2/transactions/{txid}",
        auth_header="api-key",
        secret_env="LIVE402_PQ_CONFIRM_NOWNODES_API_KEY",
    ),
}
# Optional alias for the primary (Tatum) key. Fly secret later.
PRIMARY_INDEXER_TOKEN_ENV = "LIVE402_PQ_CONFIRM_INDEXER_TOKEN"
# Provider capability is not readiness. Tatum preserved the native Falcon
# pqsig for confirmed MainNet transaction TGCEA72N... during the 2026-09-03
# activation preflight. Runtime readiness still requires a recent, durable
# proof produced by fetch+decode+verify of a confirmed Falcon anchor. NowNodes
# remains unproven and fail-closed.
CONFIRM_FALCON_COMPATIBLE = {
    "tatum": True,
    "nownodes": False,
}
CONFIRMATION_PROOF_MAX_AGE_S = 60 * 60


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
    "Production MainNet GO requires LIVE402_PQ_CONFIRM_PROVIDER=tatum "
    "(primary) or nownodes (failover) so confirm is a different org "
    "than AlgoNode/Nodely submit, plus the matching API key as a Fly "
    "secret later (LIVE402_PQ_CONFIRM_TATUM_API_KEY or "
    "LIVE402_PQ_CONFIRM_INDEXER_TOKEN; LIVE402_PQ_CONFIRM_NOWNODES_API_KEY "
    "for failover). Do not set those secrets in this PR. AlgoNode, "
    "Nodely, Allo, and Oanor are the same trust domain. Org labels "
    "come from public company records, not a legal audit. "
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


def pending_host_allowlisted(network: str, host: str) -> bool:
    allowed = PENDING_HOST_ALLOWLIST.get((network or "").strip().lower(), frozenset())
    return (host or "").strip().lower() in allowed


def _org_from_suffix(host: str) -> str:
    """Same-org / AlgoNode-backed suffixes. Public records, not a legal audit."""
    key = (host or "").strip().lower()
    if ".algonode." in key:
        return ORG_NODELY
    if ".4160.nodely." in key or key.endswith(".nodely.dev") or key.endswith(".nodely.io"):
        return ORG_NODELY
    if key == "allo.info" or key.endswith(".allo.info"):
        return ORG_NODELY
    if key == "oanor.com" or key.endswith(".oanor.com"):
        return ORG_NODELY
    return ""


def provider_org(host: str, orgs: dict | None = None) -> str:
    """Hardcoded organization for an allowlisted host. Empty if unknown.

    Unknown host => empty => not independent. A different Nodely
    hostname is still Nodely. Allo and Oanor are AlgoNode-backed.
    Custom `orgs` maps are exact-only (tests).
    """
    key = (host or "").strip().lower()
    mapping = PROVIDER_ORGS if orgs is None else orgs
    if key in mapping:
        return mapping[key]
    if orgs is not None:
        return ""
    return _org_from_suffix(key)


def confirmation_independent(
    submit_host: str,
    confirm_host: str,
    orgs: dict | None = None,
) -> bool:
    """True only when both hosts map to known, different organizations.

    same org => False
    unknown org (either side) => False
    different known orgs => True
    A different Nodely hostname is not a different org.
    This flag alone does not authorize MainNet GO.
    """
    submit_org = provider_org(submit_host, orgs=orgs)
    confirm_org = provider_org(confirm_host, orgs=orgs)
    if not submit_org or not confirm_org:
        return False
    return submit_org != confirm_org


def confirm_host_allowlisted(network: str, host: str) -> bool:
    key = (host or "").strip().lower()
    allowed = CONFIRM_HOST_ALLOWLIST.get((network or "").strip().lower(), frozenset())
    if key in allowed:
        return True
    return any(p.host == key for p in CONFIRM_PROVIDERS.values())


def configured_confirm_provider() -> ConfirmProvider | None:
    """Enum from LIVE402_PQ_CONFIRM_PROVIDER. Unknown name fails closed."""
    raw = (os.environ.get(CONFIRM_PROVIDER_ENV) or "").strip().lower()
    if not raw:
        return None
    if raw not in CONFIRM_PROVIDERS:
        raise UnknownNetwork("unknown confirm provider")
    return CONFIRM_PROVIDERS[raw]


def configured_confirm_host(network: str) -> str:
    """Effective confirm host. Static provider table or network default."""
    cfg = get_network(network)
    if cfg.name != MAINNET_NAME:
        return cfg.confirm_host
    provider = configured_confirm_provider()
    if provider is None:
        return cfg.confirm_host
    return provider.host


def configured_confirm_txn_url(network: str, txid: str = "") -> str:
    """HTTPS GET URL from the hardcoded table. No env-chosen path."""
    cfg = get_network(network)
    provider = configured_confirm_provider() if cfg.name == MAINNET_NAME else None
    if provider is None:
        if txid:
            return cfg.confirm_txn_url + txid
        return cfg.confirm_txn_url
    path = provider.path_template.replace("{txid}", txid)
    return "https://%s%s" % (provider.host, path)


def confirm_auth_header() -> tuple[str, str] | None:
    """(header_name, secret) for the selected provider. None if unset.

    Caller must not log the secret. Never place it in a URL or query.
    """
    provider = configured_confirm_provider()
    if provider is None:
        return None
    secret = (os.environ.get(provider.secret_env) or "").strip()
    if not secret and provider.name == CONFIRM_PROVIDER_PRIMARY:
        secret = (os.environ.get(PRIMARY_INDEXER_TOKEN_ENV) or "").strip()
    if not secret:
        return None
    return provider.auth_header, secret


def credentials_configured() -> bool:
    return confirm_auth_header() is not None


def runtime_confirmation_independent(network: str | None = None) -> bool:
    """Org-only. Does not mean confirmation_ready or MainNet GO."""
    name = (network or MAINNET_NAME).strip().lower()
    cfg = get_network(name)
    try:
        confirm = configured_confirm_host(cfg.name)
    except UnknownNetwork:
        return False
    return confirmation_independent(cfg.submit_host, confirm)


def _confirmation_proof_valid(
    provider: ConfirmProvider | None,
    proof: dict | None,
    *,
    now: int,
) -> tuple[bool, int | None]:
    """Validate public-only evidence of an exact Falcon confirmation fetch."""
    if provider is None or not isinstance(proof, dict):
        return False, None
    try:
        version = int(proof.get("version") or 0)
        verified_at = int(proof.get("verified_at") or 0)
        tree_size = int(proof.get("tree_size") or 0)
    except (TypeError, ValueError):
        return False, None
    root = str(proof.get("root") or "")
    txid = str(proof.get("txid") or "")
    age = int(now) - verified_at
    valid = bool(
        version == 1
        and str(proof.get("network") or "") == MAINNET_NAME
        and str(proof.get("provider") or "") == provider.name
        and str(proof.get("host") or "") == provider.host
        and str(proof.get("falcon_scheme") or "") == "f1"
        and tree_size >= 1
        and len(root) == 64
        and all(ch in "0123456789abcdef" for ch in root)
        and len(txid) == 52
        and all(ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for ch in txid)
        and verified_at >= 1
        and 0 <= age <= CONFIRMATION_PROOF_MAX_AGE_S
    )
    return valid, age if verified_at >= 1 else None


def confirmation_status(
    network: str | None = None,
    *,
    proof: dict | None = None,
    now: int | None = None,
) -> dict:
    """Split independence vs readiness. No secrets.

    confirmation_ready requires known provider, different org,
    credentials configured, a known Falcon-compatible provider, and a
    recent durable proof from exact fetch+decode+semantic verification.
    Missing any one stays false. This does not flip trust v2.
    """
    name = (network or MAINNET_NAME).strip().lower()
    cfg = get_network(name)
    provider = None
    known = False
    try:
        provider = configured_confirm_provider() if name == MAINNET_NAME else None
        known = provider is not None
    except UnknownNetwork:
        provider = None
        known = False
    host = provider.host if provider else cfg.confirm_host
    org = provider.org if provider else provider_org(host)
    org_indep = confirmation_independent(cfg.submit_host, host) if known else False
    creds = bool(provider and confirm_auth_header())
    falcon_ok = bool(provider and CONFIRM_FALCON_COMPATIBLE.get(provider.name))
    proof_ok, proof_age = _confirmation_proof_valid(
        provider,
        proof,
        now=int(now if now is not None else time.time()),
    )
    reachable = bool(proof_ok)
    ready = bool(known and org_indep and creds and reachable and falcon_ok)
    blocker = ""
    if not known:
        blocker = "confirm_provider_not_selected"
    elif not org_indep:
        blocker = "confirm_org_not_independent"
    elif not creds:
        blocker = "confirm_credentials_missing"
    elif not falcon_ok:
        blocker = "confirm_provider_falcon_unproven"
    elif not proof_ok:
        blocker = "confirm_proof_missing_or_stale"
    return {
        "confirm_provider_known": known,
        "confirm_provider": provider.name if provider else "",
        "confirm_host": host,
        "confirm_org": org,
        "confirm_org_independent": org_indep,
        "confirm_credentials_configured": creds,
        "confirm_reachable": reachable,
        "confirm_falcon_compatible": falcon_ok,
        "confirmation_ready": ready,
        "confirmation_proof_age_s": proof_age,
        "confirmation_proof_max_age_s": CONFIRMATION_PROOF_MAX_AGE_S,
        "blocker": blocker,
    }


def mainnet_confirmation_policy(cfg: NetworkConfig | None = None) -> dict:
    """Static default-host policy. Matches committed trust_root.v2.

    Default submit+confirm are both Nodely. independent_provider is
    false. Runtime independence is computed_confirmation_policy.
    """
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
        "allo_and_oanor_algonode_backed": True,
        "default_submit_and_confirm": "nodely_same_org",
        "org_independence_source": "public_company_records_not_legal_audit",
        "before_mainnet_go": (
            "Tatum primary or NowNodes failover plus API key as a "
            "Fly secret later; fetch+decode+verify still required"
        ),
    }


def computed_confirmation_policy(network: str | None = None) -> dict:
    """Runtime confirmation_policy for the configured confirm provider.

    When confirm is Tatum or NowNodes and submit is AlgoNode/Nodely,
    independent_provider is true. Default AlgoNode/Nodely confirm
    stays false. Does not rewrite committed trust_root.v2.
    """
    name = (network or MAINNET_NAME).strip().lower()
    cfg = get_network(name)
    try:
        confirm_host = configured_confirm_host(cfg.name)
    except UnknownNetwork:
        confirm_host = cfg.confirm_host
    submit_org = provider_org(cfg.submit_host)
    confirm_org = provider_org(confirm_host)
    independent = confirmation_independent(cfg.submit_host, confirm_host)
    out = mainnet_confirmation_policy(cfg)
    out["independent_provider"] = independent
    out["confirm_host"] = confirm_host
    out["confirm_org"] = confirm_org
    out["submit_host"] = cfg.submit_host
    out["submit_org"] = submit_org
    out["confirm_provider"] = ""
    try:
        provider = configured_confirm_provider() if cfg.name == MAINNET_NAME else None
        out["confirm_provider"] = provider.name if provider else ""
    except UnknownNetwork:
        out["independent_provider"] = False
    return out
