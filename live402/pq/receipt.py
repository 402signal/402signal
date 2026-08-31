"""Receipt / tlog-proof. Never sign before the leaf is durable.

Flow: canonicalize → durable append → assigned index → sign C2SP checkpoint
→ return {index, inclusion_path, checkpoint}.

pending = durable leaf + signed checkpoint, not yet Algorand-anchored.
unavailable = log could not produce that pair. Never call unavailable "pending".
Do not wait for Algorand on the request path. Never emit pq_secure:true.
"""

from __future__ import annotations

import base64
import os
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


class ReceiptError(RuntimeError):
    pass


class CrashBeforeSign(RuntimeError):
    """Test crash after durable idx, before checkpoint signature."""


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


def current_signer():
    return _signer


def install_before_append_hook(fn: Callable[[bytes], None] | None) -> None:
    _before_append_hooks.clear()
    if fn is not None:
        _before_append_hooks.append(fn)


def available() -> bool:
    if os.environ.get("LIVE402_PQ_LOG") == "0":
        return False
    return _signer is not None


def issue(event: dict) -> dict:
    """Canonicalize, durable-append, then sign. No receipt if any step fails."""
    if not available():
        raise ReceiptError("pq log unavailable")
    body = events.leaf_bytes(event)
    for hook in list(_before_append_hooks):
        hook(body)
    rec = store.append(body)
    idx = int(rec["idx"])
    tree_size = int(rec["size"])
    if store.leaf_at(idx) is None:
        raise ReceiptError("leaf not durable")
    store.publish_up_to(tree_size)
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
    }


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


def attach_to_route(result: dict, request_body: dict | None = None) -> dict:
    """Best-effort transparency object. Paid /route still succeeds if this fails."""
    if not isinstance(result, dict):
        return {}
    pay = result.get("payment_authorization")
    if not isinstance(pay, dict):
        pay = {}
    pay["pq_native"] = False
    result["payment_authorization"] = pay
    result.pop("pq_secure", None)
    origin = store.origin() or ORIGIN
    if not available():
        result["pq_trust"] = {
            "transparency": {
                "status": "unavailable",
                "log_origin": origin,
            }
        }
        return result
    try:
        req = request_body if isinstance(request_body, dict) else {}
        ev = events.route_decision_event(
            need=req.get("need") if isinstance(req.get("need"), str) else "",
            url=(req.get("url") if isinstance(req.get("url"), str) else "")
            or (result.get("url") if isinstance(result.get("url"), str) else ""),
            live=result.get("live"),
            miss_reason=result.get("miss_reason") if isinstance(result.get("miss_reason"), str) else None,
        )
        proof = issue(ev)
        result["pq_trust"] = {
            "transparency": {
                "status": "pending",
                "log_origin": origin,
                "index": proof["index"],
                "checkpoint_size": proof["checkpoint_size"],
                "receipt": {
                    "index": proof["index"],
                    "inclusion_path": proof["inclusion_path"],
                    "checkpoint": proof["checkpoint"],
                },
            }
        }
    except Exception:
        result["pq_trust"] = {
            "transparency": {
                "status": "unavailable",
                "log_origin": origin,
            }
        }
    return result
