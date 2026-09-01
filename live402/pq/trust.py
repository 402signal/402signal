"""Versioned PQ1 trust-root descriptor. Unknown algorithms fail closed.

Rotation is a new shard, never a mixed-hash tree.
Ed25519 vkey is a placeholder here; production value comes from env.
Do not commit a private key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_DESCRIPTOR = _ROOT / "trust_root.v1.json"
_DESCRIPTOR_V2 = _ROOT / "trust_root.v2.json"

SUPPORTED_MERKLE = frozenset({"SHA-256", "SHA256", "sha-256", "sha256"})
SUPPORTED_LOG_SIG = frozenset({"ed25519", "Ed25519", "ED25519"})
SUPPORTED_JCS = frozenset({"RFC8785", "rfc8785", "RFC 8785"})


class UnknownAlgorithm(ValueError):
    """Fail-closed: descriptor names an algorithm this shard does not speak."""


def descriptor_path() -> Path:
    return _DESCRIPTOR


def descriptor_path_v2() -> Path:
    return _DESCRIPTOR_V2


def load_descriptor(path: Path | None = None) -> dict:
    target = path or _DESCRIPTOR
    raw = target.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise UnknownAlgorithm("trust root must be a JSON object")
    return data


def validate_descriptor(desc: dict | None) -> dict:
    """Reject unknown merkle / log-signature / JCS algorithms."""
    if not isinstance(desc, dict):
        raise UnknownAlgorithm("missing trust root")
    merkle = desc.get("merkle") if isinstance(desc.get("merkle"), dict) else {}
    alg = str(merkle.get("algorithm") or "")
    if alg not in SUPPORTED_MERKLE:
        raise UnknownAlgorithm("unsupported merkle algorithm")
    sig = desc.get("log_signature") if isinstance(desc.get("log_signature"), dict) else {}
    sig_alg = str(sig.get("algorithm") or "")
    if sig_alg not in SUPPORTED_LOG_SIG:
        raise UnknownAlgorithm("unsupported log signature algorithm")
    jcs = desc.get("jcs") if isinstance(desc.get("jcs"), dict) else {}
    profile = str(jcs.get("profile") or "")
    if profile not in SUPPORTED_JCS:
        raise UnknownAlgorithm("unsupported JCS profile")
    if desc.get("unknown_algorithm") not in {None, "fail-closed", "fail_closed"}:
        raise UnknownAlgorithm("unknown_algorithm policy must be fail-closed")
    if str(desc.get("rotation") or "new-shard") != "new-shard":
        raise UnknownAlgorithm("rotation must be new-shard")
    return desc


def validate_descriptor_v2(desc: dict | None) -> dict:
    """Reject unknown algorithms and require MainNet-prep fields. No secrets."""
    desc = validate_descriptor(desc)
    if int(desc.get("version") or 0) != 2:
        raise UnknownAlgorithm("trust root v2 version must be 2")
    if str(desc.get("epoch") or "") != "mainnet-v1":
        raise UnknownAlgorithm("trust root v2 epoch must be mainnet-v1")
    if str(desc.get("origin") or "") != "402signal.com/pq/log/mainnet-v1":
        raise UnknownAlgorithm("trust root v2 origin must be distinct")
    falcon = desc.get("falcon") if isinstance(desc.get("falcon"), dict) else {}
    if str(falcon.get("scheme") or "") != "f1":
        raise UnknownAlgorithm("falcon scheme must be f1")
    if falcon.get("pq1") is not True:
        raise UnknownAlgorithm("falcon pq1 must be true")
    if str(falcon.get("genesis_id") or "") != "mainnet-v1.0":
        raise UnknownAlgorithm("falcon genesis_id must be mainnet-v1.0")
    if str(falcon.get("genesis_hash") or "") != "wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=":
        raise UnknownAlgorithm("falcon genesis_hash must match MainNet")
    if str(falcon.get("address") or "").strip():
        raise UnknownAlgorithm("falcon address stays empty until Ross ceremony")
    if str(falcon.get("allowed_broadcast") or "") != "none":
        raise UnknownAlgorithm("v2 allowed_broadcast must stay none")
    policy = desc.get("broadcast_policy") if isinstance(desc.get("broadcast_policy"), dict) else {}
    if str(policy.get("default") or "") != "off" or str(policy.get("automatic") or "") != "off":
        raise UnknownAlgorithm("v2 broadcast policy must stay off")
    confirm = desc.get("confirmation_policy") if isinstance(desc.get("confirmation_policy"), dict) else {}
    if str(confirm.get("require") or "") != "fetch_and_decode_actual_txn":
        raise UnknownAlgorithm("confirmation must fetch and decode the actual txn")
    if confirm.get("independent_provider") is not False:
        raise UnknownAlgorithm("v2 must not claim independent confirmation yet")
    if confirm.get("same_trust_domain_not_sufficient") is not True:
        raise UnknownAlgorithm("AlgoNode plus AlgoNode is not independent confirmation")
    if desc.get("not_mainnet_go") is not True:
        raise UnknownAlgorithm("v2 not_mainnet_go must stay true")
    sig = desc.get("log_signature") if isinstance(desc.get("log_signature"), dict) else {}
    if sig.get("sk") or sig.get("private_key") or desc.get("sk") or desc.get("private_key"):
        raise UnknownAlgorithm("secrets forbidden in trust root")
    if sig.get("reuse_testnet_sk") is not False:
        raise UnknownAlgorithm("must not reuse TestNet Ed25519 SK")
    return desc


def trust_root() -> dict:
    return validate_descriptor(load_descriptor())


def trust_root_v2() -> dict:
    """Prepared MainNet epoch descriptor. Not the live public /pq/log/trust."""
    return validate_descriptor_v2(load_descriptor(_DESCRIPTOR_V2))


def origin() -> str:
    desc = trust_root()
    return str(desc.get("origin") or "")


def vkey() -> str:
    """Public verifier key. Env wins; descriptor may hold a placeholder."""
    env = (os.environ.get("LIVE402_PQ_LOG_VKEY") or "").strip()
    if env:
        return env
    desc = trust_root()
    sig = desc.get("log_signature") if isinstance(desc.get("log_signature"), dict) else {}
    return str(sig.get("vkey") or "").strip()


def falcon_address() -> str:
    env_name = "LIVE402_PQ_FALCON_ADDRESS"
    desc = trust_root()
    falcon = desc.get("falcon") if isinstance(desc.get("falcon"), dict) else {}
    env_name = str(falcon.get("address_env") or env_name)
    return (os.environ.get(env_name) or "").strip()


def falcon_allowed_broadcast() -> str:
    """Only TestNet may be broadcast. MainNet stays behind not_mainnet_go."""
    desc = trust_root()
    falcon = desc.get("falcon") if isinstance(desc.get("falcon"), dict) else {}
    return str(falcon.get("allowed_broadcast") or "testnet").strip().lower()


def witness_policy() -> list:
    desc = trust_root()
    policy = desc.get("witness_policy")
    return list(policy) if isinstance(policy, list) else []


def public_descriptor() -> dict:
    """Public trust descriptor. TestNet only. Runtime PUBLIC vkey. Never a private key.

    Empty witness_policy stays empty. vkey comes from LIVE402_PQ_LOG_VKEY or
    the in-memory store meta after boot. Never fabricated.
    """
    desc = dict(trust_root())
    falcon = dict(desc.get("falcon") or {})
    falcon["network"] = "testnet-v1.0"
    falcon["allowed_broadcast"] = "testnet"
    desc["falcon"] = falcon
    desc["not_mainnet_go"] = True
    desc["witness_policy"] = witness_policy()
    runtime = vkey()
    sig = dict(desc.get("log_signature") or {})
    sig["vkey"] = runtime
    sig.pop("sk", None)
    sig.pop("private_key", None)
    desc["log_signature"] = sig
    desc.pop("private_key", None)
    desc.pop("sk", None)
    return desc
