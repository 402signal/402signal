"""Receipt / tlog-proof. Never sign before the leaf is durable.

Flow: canonicalize → durable append (independent of Ed25519) → assigned
index → optional sign C2SP checkpoint → return receipt.

States: logged_uncheckpointed → checkpoint_signed → authorized →
submitted → confirmed. Public status "pending" still means durable leaf
+ signed checkpoint (checkpoint_signed), not an Algorand inclusion.
Never say pending if the leaf is not durable. Never say signed if there
is no checkpoint. unavailable = could not append. Do not wait for
Algorand on the request path. Never emit pq_secure:true.
"""

from __future__ import annotations

import base64
import os
import sys
from collections.abc import Callable
from typing import Any

from live402.pq import ORIGIN
from live402.pq import checkpoint as ckpt
from live402.pq import events
from live402.pq import merkle
from live402.pq import store
from live402.pq import trust

_signer: Any = None
_before_append_hooks: list[Callable[[bytes], None]] = []

# Env name only. Never interpolate the value into logs or exceptions.
_SK_ENV = "LIVE402_PQ_LOG_SK"
_VKEY_ENV = "LIVE402_PQ_LOG_VKEY"


class ReceiptError(RuntimeError):
    pass


class CrashBeforeSign(RuntimeError):
    """Test crash after durable idx, before checkpoint signature."""


class SignerConfigError(ValueError):
    """LIVE402_PQ_LOG_SK was set but could not be parsed. Do not generate a key."""


def configure_signer(private_key: Any = None) -> str:
    """Install an in-memory Ed25519 log key. Never log or serialize the private key."""
    global _signer
    _signer = private_key
    if private_key is None:
        store.meta_set("vkey", "")
        return ""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    pk = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    vkey = ckpt.vkey_encode(ORIGIN, pk)
    store.meta_set("vkey", vkey)
    return vkey


def _parse_log_sk(raw: str) -> Any:
    """Parse a 32-byte hex seed or PKCS8 PEM. Raises SignerConfigError if malformed.

    Never include the secret in the exception message.
    """
    text = (raw or "").strip()
    if not text:
        raise SignerConfigError("empty")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError as exc:
        raise SignerConfigError("cryptography unavailable") from exc
    if "BEGIN" in text and "PRIVATE KEY" in text:
        try:
            key = load_pem_private_key(text.encode("utf-8"), password=None)
        except Exception as exc:
            raise SignerConfigError("malformed pem") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise SignerConfigError("not ed25519")
        return key
    hex_s = text.lower()
    if hex_s.startswith("0x"):
        hex_s = hex_s[2:]
    hex_s = "".join(hex_s.split())
    if len(hex_s) != 64:
        raise SignerConfigError("not 32-byte hex")
    try:
        seed = bytes.fromhex(hex_s)
    except ValueError as exc:
        raise SignerConfigError("not hex") from exc
    if len(seed) != 32:
        raise SignerConfigError("not 32-byte hex")
    try:
        return Ed25519PrivateKey.from_private_bytes(seed)
    except Exception as exc:
        raise SignerConfigError("invalid seed") from exc


def load_signer_from_env() -> str:
    """Load LIVE402_PQ_LOG_SK into memory. Never generate a key.

    Unset or blank: leave signer unconfigured (receipts stay unavailable).
    Malformed: fail closed — clear any signer, do not generate, keep serving.
    Success: configure_signer + store vkey + LIVE402_PQ_LOG_VKEY from the public key.
    Never logs, prints, or writes the secret.
    """
    raw = os.environ.get(_SK_ENV)
    if raw is None or not str(raw).strip():
        return ""
    try:
        key = _parse_log_sk(raw)
    except Exception:
        configure_signer(None)
        sys.stderr.write("%s malformed; log signing disabled\n" % _SK_ENV)
        return ""
    vkey = configure_signer(key)
    if vkey:
        os.environ[_VKEY_ENV] = vkey
    return vkey


def current_signer():
    return _signer


def install_before_append_hook(fn: Callable[[bytes], None] | None) -> None:
    _before_append_hooks.clear()
    if fn is not None:
        _before_append_hooks.append(fn)


def log_enabled() -> bool:
    """True when the append-only log may accept a leaf. Independent of Ed25519."""
    return os.environ.get("LIVE402_PQ_LOG") != "0"


def available() -> bool:
    """True when a signed checkpoint receipt can be issued."""
    return log_enabled() and _signer is not None


def append_event(event: dict) -> dict:
    """Durable append only. Does not sign. Independent of Ed25519."""
    if not log_enabled():
        raise ReceiptError("pq log unavailable")
    body = events.leaf_bytes(event)
    for hook in list(_before_append_hooks):
        hook(body)
    rec = store.append(body)
    idx = int(rec["idx"])
    if store.leaf_at(idx) is None:
        raise ReceiptError("leaf not durable")
    store.publish_up_to(int(rec["size"]))
    return rec


def issue(event: dict) -> dict:
    """Canonicalize, durable-append, then sign. No signed receipt if signing fails."""
    rec = append_event(event)
    idx = int(rec["idx"])
    tree_size = int(rec["size"])
    if not store.ready_to_checkpoint(tree_size):
        raise ReceiptError("tiles/bundles missing; refusing to sign")
    signer = _signer
    if signer is None:
        raise ReceiptError("pq log unavailable")
    root = store.root(tree_size)
    note = ckpt.sign_checkpoint(store.origin() or ORIGIN, tree_size, root, signer)
    store.save_checkpoint(tree_size, note)
    path = store.inclusion_path(idx, tree_size)
    if not merkle.verify_inclusion(idx, rec["leaf_hash"], path, root, tree_size):
        raise ReceiptError("inclusion proof failed")
    return {
        "index": idx,
        "inclusion_path": [base64.b64encode(p).decode("ascii") for p in path],
        "checkpoint": note,
        "checkpoint_size": tree_size,
        "leaf_hash": rec["leaf_hash"].hex(),
        "state": "checkpoint_signed",
    }


def verify_route_receipt(receipt: dict, reveal: dict, vkey: str | None = None) -> dict:
    """Fail-closed v3 receipt check: event version, reveal, commitment, leaf, inclusion, Ed25519."""
    if not isinstance(receipt, dict) or not isinstance(reveal, dict):
        raise ReceiptError("invalid receipt")
    version = reveal.get("event_version") or reveal.get("type")
    if version != events.TYPE_ROUTE_DECISION_V3:
        raise ReceiptError("unsupported event version")
    commitment = reveal.get("commitment")
    if not isinstance(commitment, str) or len(commitment) != 64:
        raise ReceiptError("missing commitment")
    evidence = reveal.get("evidence")
    salt = reveal.get("salt")
    nonce = reveal.get("nonce")
    ts = reveal.get("ts")
    if not isinstance(evidence, dict) or not salt or not nonce or not ts:
        raise ReceiptError("missing reveal fields")
    if not events.verify_reveal_v3(commitment, reveal):
        raise ReceiptError("reveal mismatch")
    leaf_hex = receipt.get("leaf_hash")
    if not isinstance(leaf_hex, str) or not leaf_hex:
        raise ReceiptError("missing leaf hash")
    public_leaf = {
        "commitment": commitment.lower(),
        "nonce": nonce,
        "ts": ts,
        "type": events.TYPE_ROUTE_DECISION_V3,
    }
    try:
        body = events.leaf_bytes(public_leaf)
    except (events.PrivacyError, ValueError, TypeError) as exc:
        raise ReceiptError("invalid public leaf") from exc
    expected_leaf = merkle.leaf_hash(body).hex()
    if expected_leaf != leaf_hex.lower():
        raise ReceiptError("leaf hash mismatch")
    return verify_receipt(receipt, vkey)


def verify_receipt(receipt: dict, vkey: str | None = None) -> dict:
    if not isinstance(receipt, dict):
        raise ReceiptError("invalid receipt")
    note = receipt.get("checkpoint")
    vk = vkey or store.meta_get("vkey") or trust.vkey()
    if not note or not vk:
        raise ReceiptError("missing checkpoint or vkey")
    try:
        verified = ckpt.verify_signed_note(note, vk)
    except ValueError as exc:
        raise ReceiptError("checkpoint verify failed") from exc
    body = verified["body"]
    idx = int(receipt.get("index"))
    try:
        path = [base64.b64decode(p, validate=True) for p in (receipt.get("inclusion_path") or [])]
    except (TypeError, ValueError) as exc:
        raise ReceiptError("corrupt inclusion path") from exc
    leaf_hex = receipt.get("leaf_hash") or ""
    try:
        leaf_h = bytes.fromhex(leaf_hex)
    except ValueError as exc:
        raise ReceiptError("corrupt leaf hash") from exc
    if not merkle.verify_inclusion(idx, leaf_h, path, body["root"], body["tree_size"]):
        raise ReceiptError("corrupt proof")
    return verified


def _unavailable(result: dict, origin: str) -> dict:
    result["pq_trust"] = {
        "transparency": {
            "status": "unavailable",
            "state": "unavailable",
            "log_origin": origin,
        }
    }
    return result


def attach_to_route(result: dict, request_body: dict | None = None) -> dict:
    """Best-effort transparency object. Paid /route still succeeds if this fails
    unless the caller set require_transparency (handled by route.py).
    """
    if not isinstance(result, dict):
        return {}
    pay = result.get("payment_authorization")
    if not isinstance(pay, dict):
        pay = {}
    pay["pq_native"] = False
    result["payment_authorization"] = pay
    result.pop("pq_secure", None)
    origin = store.origin() or ORIGIN
    if not log_enabled():
        return _unavailable(result, origin)
    try:
        req = request_body if isinstance(request_body, dict) else {}
        evidence = events.private_evidence_v3_from_route(result, req)
        ev, reveal = events.route_decision_event_v3(evidence=evidence)
        transparency = {
            "log_origin": origin,
            "leaf_type": events.TYPE_ROUTE_DECISION_V3,
            "reveal": reveal,
        }
        if available():
            try:
                proof = issue(ev)
                transparency.update(
                    {
                        "status": "pending",
                        "state": "checkpoint_signed",
                        "index": proof["index"],
                        "checkpoint_size": proof["checkpoint_size"],
                        "receipt": {
                            "index": proof["index"],
                            "inclusion_path": proof["inclusion_path"],
                            "checkpoint": proof["checkpoint"],
                            "leaf_hash": proof["leaf_hash"],
                        },
                    }
                )
            except ReceiptError:
                rec = append_event(ev)
                transparency.update(
                    {
                        "status": "logged_uncheckpointed",
                        "state": "logged_uncheckpointed",
                        "index": rec["idx"],
                        "receipt": {"index": rec["idx"], "leaf_hash": rec["leaf_hash"].hex()},
                    }
                )
        else:
            rec = append_event(ev)
            transparency.update(
                {
                    "status": "logged_uncheckpointed",
                    "state": "logged_uncheckpointed",
                    "index": rec["idx"],
                    "receipt": {"index": rec["idx"], "leaf_hash": rec["leaf_hash"].hex()},
                }
            )
        result["pq_trust"] = {"transparency": transparency}
    except Exception:
        return _unavailable(result, origin)
    return result
