"""Fresh production log identity vs the archived TestNet shard.

Production live path uses MainNet epoch mainnet-v1 (distinct origin, vkey, sqlite).
Do not copy TestNet leaves into the production tree.
Do not delete the TestNet DB archive.

Unknown or typo epoch/network values raise ConfigError. They never
silently become TestNet. NETWORK=mainnet requires the full MainNet
identity (epoch, DB path, origin, trust v2, Falcon address, signer).
"""

from __future__ import annotations

import os

from live402.pq import (
    DEFAULT_DB,
    DEFAULT_DB_MAINNET,
    ORIGIN,
    ORIGIN_MAINNET,
    VOLUME_DB,
    VOLUME_DB_MAINNET,
)

EPOCH_ENV = "LIVE402_PQ_LOG_EPOCH"
ORIGIN_ENV = "LIVE402_PQ_LOG_ORIGIN"
NETWORK_ENV = "LIVE402_PQ_FALCON_NETWORK"
EPOCH_TESTNET = "testnet-v1"
EPOCH_MAINNET = "mainnet-v1"
NETWORK_TESTNET = "testnet"
NETWORK_MAINNET = "mainnet"
MAINNET_ADDRESS_ENV = "LIVE402_PQ_FALCON_MAINNET_ADDRESS"
MAINNET_SIGNER_TOKEN_ENV = "LIVE402_PQ_SIGNER_MAINNET_TOKEN"
MAINNET_SK_ENV = "LIVE402_PQ_LOG_SK_MAINNET"
MAINNET_VKEY_ENV = "LIVE402_PQ_LOG_VKEY_MAINNET"
TESTNET_SK_ENV = "LIVE402_PQ_LOG_SK"
TESTNET_DB_NAME = "pq-log.sqlite"
MAINNET_DB_NAME = "pq-log-mainnet.sqlite"


class ConfigError(ValueError):
    """Fail closed: epoch/network/identity is unknown or cross-configured."""


def configured_epoch() -> str:
    raw = (os.environ.get(EPOCH_ENV) or "").strip().lower()
    if raw == "":
        return EPOCH_TESTNET
    if raw not in {EPOCH_TESTNET, EPOCH_MAINNET}:
        raise ConfigError("unknown epoch")
    return raw


def is_mainnet_epoch() -> bool:
    return configured_epoch() == EPOCH_MAINNET


def configured_network() -> str:
    """Exact configured value. Unset is empty (live TestNet identity).

    Typos raise. Unset does not become TestNet for submit gates.
    NETWORK=mainnet still requires require_mainnet_identity().
    """
    raw = (os.environ.get(NETWORK_ENV) or "").strip().lower()
    if raw == "":
        return ""
    if raw not in {NETWORK_TESTNET, NETWORK_MAINNET}:
        raise ConfigError("unknown network")
    return raw


def configured_origin() -> str:
    raw = (os.environ.get(ORIGIN_ENV) or "").strip()
    epoch = configured_epoch()
    network = configured_network()
    if network == NETWORK_MAINNET and epoch != EPOCH_MAINNET:
        raise ConfigError("mainnet requires epoch mainnet-v1")
    if epoch == EPOCH_MAINNET:
        if raw and raw != ORIGIN_MAINNET:
            raise ConfigError("mainnet origin mismatch")
        return ORIGIN_MAINNET
    if raw == ORIGIN_MAINNET:
        raise ConfigError("mainnet origin requires epoch mainnet-v1")
    if raw:
        return raw
    return ORIGIN


def db_path_kind(path: str) -> str:
    name = os.path.basename(path or "")
    if name == MAINNET_DB_NAME:
        return "mainnet"
    if name == TESTNET_DB_NAME:
        return "testnet"
    return "unknown"


def default_db_for_epoch() -> str:
    """Fallback path when LIVE402_PQ_LOG_DB is unset.

    Production fly.toml sets LIVE402_PQ_LOG_DB to the MainNet file.
    A MainNet epoch without an explicit DB still uses a distinct file.
    It never reuses pq-log.sqlite.
    """
    if is_mainnet_epoch():
        try:
            if os.path.isdir("/data") and os.access("/data", os.W_OK):
                return VOLUME_DB_MAINNET
        except Exception:
            pass
        return DEFAULT_DB_MAINNET
    try:
        if os.path.isdir("/data") and os.access("/data", os.W_OK):
            return VOLUME_DB
    except Exception:
        pass
    return DEFAULT_DB


def resolve_db_path(explicit: str | None = None) -> str:
    """Resolve sqlite path. MainNet cannot select the TestNet file.

    NETWORK=mainnet and epoch=mainnet-v1 both fail closed against
    pq-log.sqlite. Typos already raise in configured_epoch/network.
    """
    raw = (explicit if explicit is not None else (os.environ.get("LIVE402_PQ_LOG_DB") or "")).strip()
    path = raw or default_db_for_epoch()
    epoch = configured_epoch()
    network = configured_network()
    kind = db_path_kind(path)
    if network == NETWORK_MAINNET and epoch != EPOCH_MAINNET:
        raise ConfigError("mainnet requires epoch mainnet-v1")
    if (epoch == EPOCH_MAINNET or network == NETWORK_MAINNET) and kind == "testnet":
        raise ConfigError("mainnet cannot use TestNet database")
    if network == NETWORK_MAINNET and kind != "mainnet":
        raise ConfigError("mainnet cannot use TestNet database")
    return path


def live_network_name() -> str:
    """Identity network. Unset means TestNet live path. Typos already raised."""
    raw = configured_network()
    return raw or NETWORK_TESTNET


def mainnet_falcon_address_configured() -> bool:
    return bool((os.environ.get(MAINNET_ADDRESS_ENV) or "").strip())


def mainnet_signer_configured() -> bool:
    return bool((os.environ.get(MAINNET_SIGNER_TOKEN_ENV) or "").strip())


def require_mainnet_identity(*, db_path: str, origin: str) -> None:
    """NETWORK=mainnet requires the full MainNet identity. Fail closed."""
    if configured_network() != NETWORK_MAINNET:
        raise ConfigError("network is not mainnet")
    if configured_epoch() != EPOCH_MAINNET:
        raise ConfigError("mainnet requires epoch mainnet-v1")
    if db_path_kind(db_path) != "mainnet":
        raise ConfigError("mainnet cannot use TestNet database")
    if (origin or "") != ORIGIN_MAINNET:
        raise ConfigError("mainnet cannot use TestNet origin")
    from live402.pq import trust

    desc = trust.trust_root_v2()
    sig = desc.get("log_signature") if isinstance(desc.get("log_signature"), dict) else {}
    if sig.get("reuse_testnet_sk") is not False:
        raise ConfigError("reuse_testnet_sk must be false")
    if not mainnet_falcon_address_configured():
        raise ConfigError("mainnet falcon address required")
    if not mainnet_signer_configured():
        raise ConfigError("mainnet signer required")


def reject_reused_ed25519_vkey(testnet_vkey: str, mainnet_vkey: str) -> None:
    """Public-only cutover check. Fail if MainNet vkey matches TestNet.

    Compares Ed25519 public key bytes. Never takes a secret.
    """
    from live402.pq import checkpoint as ckpt

    try:
        test_pk = ckpt.vkey_parse(testnet_vkey)["public_key"]
        main_pk = ckpt.vkey_parse(mainnet_vkey)["public_key"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ConfigError("invalid ed25519 vkey") from exc
    if test_pk == main_pk:
        raise ConfigError("mainnet ed25519 reuses testnet public key")


def testnet_volume_db() -> str:
    return VOLUME_DB


def mainnet_volume_db() -> str:
    return VOLUME_DB_MAINNET
