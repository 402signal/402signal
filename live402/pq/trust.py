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

SUPPORTED_MERKLE = frozenset({"SHA-256", "SHA256", "sha-256", "sha256"})
SUPPORTED_LOG_SIG = frozenset({"ed25519", "Ed25519", "ED25519"})
SUPPORTED_JCS = frozenset({"RFC8785", "rfc8785", "RFC 8785"})


class UnknownAlgorithm(ValueError):
    """Fail-closed: descriptor names an algorithm this shard does not speak."""


def descriptor_path() -> Path:
    return _DESCRIPTOR


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


def trust_root() -> dict:
    return validate_descriptor(load_descriptor())


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
