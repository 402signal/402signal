"""CDP facilitator JWT. Never stores payment keys; only optional CDP API credentials from env."""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from urllib.parse import urlparse


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def cdp_access_token() -> str:
    return (os.environ.get("CDP_ACCESS_TOKEN") or "").strip()


def cdp_api_key_id() -> str:
    return (
        os.environ.get("CDP_API_KEY_ID")
        or os.environ.get("CDP_API_KEY_NAME")
        or os.environ.get("CDP_KEY_ID")
        or ""
    ).strip()


def cdp_api_key_secret() -> str:
    raw = (
        os.environ.get("CDP_API_KEY_SECRET")
        or os.environ.get("CDP_KEY_SECRET")
        or ""
    )
    return raw.replace("\\n", "\n").strip()


def configured() -> bool:
    return bool(cdp_access_token() or (cdp_api_key_id() and cdp_api_key_secret()))


def _jwt_envelope(method: str, url: str, alg: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or "api.cdp.coinbase.com"
    path = parsed.path or "/"
    uri = "%s %s%s" % (method.upper(), host, path)
    now = int(time.time())
    kid = cdp_api_key_id()
    header = {
        "alg": alg,
        "typ": "JWT",
        "kid": kid,
        "nonce": secrets.token_hex(16),
    }
    claims = {
        "sub": kid,
        "iss": "cdp",
        "aud": ["cdp_service"],
        "nbf": now,
        "exp": now + 120,
        "uri": uri,
    }
    return (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    )


def _sign_ed25519(secret: str, message: bytes) -> bytes | None:
    try:
        decoded = base64.b64decode(secret, validate=False)
    except Exception:
        return None
    if len(decoded) not in {32, 64}:
        return None
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        return None
    key = Ed25519PrivateKey.from_private_bytes(decoded[:32])
    return key.sign(message)


def _sign_es256(secret: str, message: bytes) -> bytes | None:
    if "BEGIN" not in secret:
        return None
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError:
        return None
    key = load_pem_private_key(secret.encode("utf-8"), password=None)
    der = key.sign(message, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def bearer_for(method: str, url: str) -> str | None:
    """Return a Bearer token for this CDP request, or None if auth is not configured."""
    ready = cdp_access_token()
    if ready:
        return ready
    if not (cdp_api_key_id() and cdp_api_key_secret()):
        return None
    secret = cdp_api_key_secret()
    if "BEGIN" in secret:
        envelope = _jwt_envelope(method, url, "ES256")
        sig = _sign_es256(secret, envelope.encode("ascii"))
    else:
        envelope = _jwt_envelope(method, url, "EdDSA")
        sig = _sign_ed25519(secret, envelope.encode("ascii"))
    if not sig:
        return None
    return envelope + "." + _b64url(sig)
