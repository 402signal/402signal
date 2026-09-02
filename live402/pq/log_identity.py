"""Fresh production log identity vs archived TestNet TEST SUPPORT.

PRODUCTION (public router, not fixture):
  LIVE402_PQ_FALCON_NETWORK=mainnet
  LIVE402_PQ_LOG_EPOCH=mainnet-v1
  DB /data/pq-log-mainnet.sqlite
  ORIGIN 402signal.com/pq/log/mainnet-v1
  MainNet Falcon address
  MainNet signer HMAC (authorize path)
  pq-anchor/3 only (authenticated success response)

Unset or unknown network/epoch fail closed. They never become TestNet.

TEST SUPPORT (tests and archive only):
  LIVE402_FIXTURE=1 or LIVE402_PQ_TEST_SUPPORT=1
  Explicit NETWORK=testnet and EPOCH=testnet-v1 are allowed.
  TestNet constants, pq-anchor/1, LIVE402_PQ_LOG_SK, and
  LIVE402_PQ_SIGNER_TOKEN stay here. They are not a production fallback.

Do not copy TestNet leaves into the production tree.
Do not delete the TestNet DB archive.
"""

from __future__ import annotations

import hashlib
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
TEST_SUPPORT_ENV = "LIVE402_PQ_TEST_SUPPORT"
FIXTURE_ENV = "LIVE402_FIXTURE"
EPOCH_TESTNET = "testnet-v1"
EPOCH_MAINNET = "mainnet-v1"
NETWORK_TESTNET = "testnet"
NETWORK_MAINNET = "mainnet"
MAINNET_ADDRESS_ENV = "LIVE402_PQ_FALCON_MAINNET_ADDRESS"
MAINNET_SIGNER_TOKEN_ENV = "LIVE402_PQ_SIGNER_MAINNET_TOKEN"
MAINNET_SK_ENV = "LIVE402_PQ_LOG_SK_MAINNET"
MAINNET_VKEY_ENV = "LIVE402_PQ_LOG_VKEY_MAINNET"
TESTNET_SK_ENV = "LIVE402_PQ_LOG_SK"
TESTNET_VKEY_ENV = "LIVE402_PQ_LOG_VKEY"
TESTNET_SIGNER_TOKEN_ENV = "LIVE402_PQ_SIGNER_TOKEN"
TESTNET_ADDRESS_ENV = "LIVE402_PQ_FALCON_ADDRESS"
TESTNET_BROADCAST_ENV = "LIVE402_PQ_FALCON_BROADCAST"
TESTNET_DB_NAME = "pq-log.sqlite"
MAINNET_DB_NAME = "pq-log-mainnet.sqlite"
# Public TestNet Falcon f1 address. Compare only. Never a keyfile.
TESTNET_FALCON_PUBLIC_ADDRESS = (
    "OBHYXCUVOLSTZVBN5JUFIYBD4X4ZFIAFZMWMU2P45VBYGWT26MV34IFFIU"
)


class ConfigError(ValueError):
    """Fail closed: epoch/network/identity is unknown or cross-configured."""


def is_test_support() -> bool:
    """True only for tests/archive. Never a production fallback."""
    if (os.environ.get(FIXTURE_ENV) or "").strip() == "1":
        return True
    if (os.environ.get(TEST_SUPPORT_ENV) or "").strip() == "1":
        return True
    return False


def is_production_runtime() -> bool:
    """Public router path. TestNet is not a live fallback."""
    return not is_test_support()


def configured_epoch() -> str:
    raw = (os.environ.get(EPOCH_ENV) or "").strip().lower()
    if raw == "":
        if is_test_support():
            return EPOCH_TESTNET
        raise ConfigError("epoch unset")
    if raw not in {EPOCH_TESTNET, EPOCH_MAINNET}:
        raise ConfigError("unknown epoch")
    return raw


def is_mainnet_epoch() -> bool:
    return configured_epoch() == EPOCH_MAINNET


def configured_network() -> str:
    """Exact configured value. Unset/unknown fail closed.

    TEST SUPPORT may omit NETWORK; that is not a production default.
    Production unset never becomes TestNet.
    """
    raw = (os.environ.get(NETWORK_ENV) or "").strip().lower()
    if raw == "":
        if is_test_support():
            return ""
        raise ConfigError("network unset")
    if raw not in {NETWORK_TESTNET, NETWORK_MAINNET}:
        raise ConfigError("unknown network")
    return raw


def configured_origin() -> str:
    raw = (os.environ.get(ORIGIN_ENV) or "").strip()
    epoch = configured_epoch()
    network = configured_network()
    if is_production_runtime() or network == NETWORK_MAINNET:
        if network and network != NETWORK_MAINNET:
            raise ConfigError("production requires network mainnet")
        if network == NETWORK_MAINNET and epoch != EPOCH_MAINNET:
            raise ConfigError("mainnet requires epoch mainnet-v1")
        if epoch == EPOCH_MAINNET:
            if raw and raw != ORIGIN_MAINNET:
                raise ConfigError("mainnet origin mismatch")
            return ORIGIN_MAINNET
        if is_production_runtime():
            raise ConfigError("production requires epoch mainnet-v1")
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

    Production uses the MainNet file. TEST SUPPORT may use the
    archived TestNet name. Production never reuses pq-log.sqlite.
    """
    if is_production_runtime() or is_mainnet_epoch():
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

    PRODUCTION and NETWORK=mainnet fail closed against pq-log.sqlite.
    """
    raw = (explicit if explicit is not None else (os.environ.get("LIVE402_PQ_LOG_DB") or "")).strip()
    path = raw or default_db_for_epoch()
    epoch = configured_epoch()
    network = configured_network()
    kind = db_path_kind(path)
    if is_production_runtime() and network != NETWORK_MAINNET:
        raise ConfigError("production requires network mainnet")
    if network == NETWORK_MAINNET and epoch != EPOCH_MAINNET:
        raise ConfigError("mainnet requires epoch mainnet-v1")
    if (epoch == EPOCH_MAINNET or network == NETWORK_MAINNET or is_production_runtime()) and kind == "testnet":
        raise ConfigError("mainnet cannot use TestNet database")
    if (network == NETWORK_MAINNET or is_production_runtime()) and kind != "mainnet":
        raise ConfigError("mainnet cannot use TestNet database")
    return path


def live_network_name() -> str:
    """Identity network. Unset fails closed in production."""
    raw = configured_network()
    if raw:
        return raw
    if is_test_support():
        return NETWORK_TESTNET
    raise ConfigError("network unset")


def mainnet_falcon_address_configured() -> bool:
    return bool((os.environ.get(MAINNET_ADDRESS_ENV) or "").strip())


def mainnet_signer_configured() -> bool:
    return bool((os.environ.get(MAINNET_SIGNER_TOKEN_ENV) or "").strip())


def production_retired_testnet_envs() -> tuple[str, ...]:
    """Env names that must not drive production PQ."""
    return (
        TESTNET_SK_ENV,
        TESTNET_SIGNER_TOKEN_ENV,
        TESTNET_BROADCAST_ENV,
        TESTNET_ADDRESS_ENV,
    )


def require_production_boot() -> None:
    """HTTP boot identity. Fail closed. Does not require signer HMAC.

    Signer HMAC is required before any MainNet authorize. Automatic
    anchoring stays off. /route still serves if this raises only when
    the caller treats it as fatal; boot records and refuses PQ live path.
    """
    if is_test_support():
        raise ConfigError("test support is not production")
    if configured_network() != NETWORK_MAINNET:
        raise ConfigError("production requires network mainnet")
    if configured_epoch() != EPOCH_MAINNET:
        raise ConfigError("production requires epoch mainnet-v1")
    db = resolve_db_path()
    if db_path_kind(db) != "mainnet":
        raise ConfigError("production cannot use TestNet database")
    if configured_origin() != ORIGIN_MAINNET:
        raise ConfigError("production cannot use TestNet origin")
    if not mainnet_falcon_address_configured():
        raise ConfigError("mainnet falcon address required")
    from live402.pq import trust

    desc = trust.trust_root_v2()
    sig = desc.get("log_signature") if isinstance(desc.get("log_signature"), dict) else {}
    if sig.get("reuse_testnet_sk") is not False:
        raise ConfigError("reuse_testnet_sk must be false")


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
    mainnet_addr = (os.environ.get(MAINNET_ADDRESS_ENV) or "").strip()
    reject_reused_falcon_address(TESTNET_FALCON_PUBLIC_ADDRESS, mainnet_addr)
    if not mainnet_signer_configured():
        raise ConfigError("mainnet signer required")


def public_string_digest(text: str) -> str:
    """SHA-256 hex of a public string. Never a secret helper."""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def reject_reused_falcon_address(testnet_addr: str, mainnet_addr: str) -> None:
    """Public-address-only. Never opens a Falcon keyfile."""
    test = (testnet_addr or "").strip()
    main = (mainnet_addr or "").strip()
    if not test or not main:
        raise ConfigError("falcon address required")
    if test == main:
        raise ConfigError("mainnet falcon reuses testnet address")


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


def reject_compromised_ed25519_vkey(candidate_vkey: str, compromised_digest: str) -> None:
    """Digest-only check against a recorded compromised PUBLIC vkey.

    `compromised_digest` is sha256(hex) of the public vkey string.
    Never takes a secret. Never prints the candidate unless the caller does.
    """
    digest = (compromised_digest or "").strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ConfigError("invalid vkey digest")
    if public_string_digest(candidate_vkey) == digest:
        raise ConfigError("mainnet ed25519 matches compromised vkey")


def testnet_volume_db() -> str:
    return VOLUME_DB


def mainnet_volume_db() -> str:
    return VOLUME_DB_MAINNET
