"""Algorand Falcon *construction* only. Never broadcast. Never emit a private key.

Note 84 bytes = ASCII "402sg/pq1:b" || 0x01 || SHA-256(UTF-8 origin) ||
uint64 BE tree_size || 32-byte RFC 6962 root.

Txn = PaymentTxn amount=0, receiver=sender, flat fee >= 3000 µALGO.
Isolated signer: unsigned txn in, pqsig out via callback.
NEVER call algod send. Falcon f1 is only for this txn, not the log signature.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from live402 import algo_tx
from live402.pq import NOTE_FORMAT, NOTE_VERSION, ORIGIN
from live402.pq import checkpoint as ckpt
from live402.pq.merkle import HASH_SIZE

NOTE_PREFIX = NOTE_FORMAT.encode("ascii")  # 11 bytes
NOTE_LEN = 84
MIN_FEE = 3000
ANCHOR_STATUSES = frozenset({"pending", "unavailable"})


class AnchorError(ValueError):
    pass


def origin_hash(origin: str) -> bytes:
    text = (origin or "").replace("\n", "")
    return hashlib.sha256(text.encode("utf-8")).digest()


def encode_note(origin: str, tree_size: int, root: bytes) -> bytes:
    if tree_size < 0:
        raise AnchorError("negative tree size")
    root_b = bytes(root)
    if len(root_b) != HASH_SIZE:
        raise AnchorError("root must be 32 bytes")
    note = (
        NOTE_PREFIX
        + bytes([NOTE_VERSION])
        + origin_hash(origin)
        + int(tree_size).to_bytes(8, "big")
        + root_b
    )
    if len(note) != NOTE_LEN:
        raise AnchorError("note must be 84 bytes")
    return note


def decode_note(note: bytes) -> dict:
    raw = bytes(note)
    if len(raw) != NOTE_LEN:
        raise AnchorError("note must be 84 bytes")
    if raw[:11] != NOTE_PREFIX or raw[11] != NOTE_VERSION:
        raise AnchorError("unknown note format")
    return {
        "format": NOTE_FORMAT,
        "version": NOTE_VERSION,
        "origin_hash": raw[12:44],
        "tree_size": int.from_bytes(raw[44:52], "big"),
        "root": raw[52:84],
    }


def c2sp_body_from_note(note: bytes, origin: str) -> str:
    """Round-trip: note + origin → C2SP checkpoint body."""
    parsed = decode_note(note)
    if parsed["origin_hash"] != origin_hash(origin):
        raise AnchorError("origin hash mismatch")
    return ckpt.checkpoint_body(origin, parsed["tree_size"], parsed["root"])


def note_from_checkpoint_body(body_text: str) -> bytes:
    parsed = ckpt.parse_checkpoint_body(body_text)
    return encode_note(parsed["origin"], parsed["tree_size"], parsed["root"])


def build_payment_txn(sender: str, note: bytes, params: dict | None = None) -> dict:
    """Unsigned PaymentTxn. amount=0, receiver=sender, fee >= 3000. Not submitted."""
    if not sender:
        raise AnchorError("falcon address required")
    p = params if isinstance(params, dict) else {}
    first = int(p.get("firstValid") or p.get("firstRound") or 1)
    last = int(p.get("lastValid") or p.get("lastRound") or (first + 1000))
    gen = str(p.get("genesisID") or p.get("genesis_id") or "mainnet-v1.0")
    gh = p.get("genesisHash") or p.get("genesis_hash")
    if isinstance(gh, str):
        import base64

        gh = base64.b64decode(gh)
    if not gh:
        import base64

        from live402 import algod as algod_mod

        gh = base64.b64decode(algod_mod.GENESIS_HASH)
    fee = max(int(p.get("fee") or p.get("minFee") or MIN_FEE), MIN_FEE)
    txn = algo_tx.pay_txn(sender, sender, 0, fee, first, last, gen, gh, note=note)
    txn["flatFee"] = True
    return txn


def isolated_sign(unsigned_txn: dict, signer_callback: Callable, pk: bytes | None = None) -> bytes:
    """unsigned txn in, pqsig out. Prefer py-algorand-sdk Falcon signer if present."""
    if not callable(signer_callback):
        raise AnchorError("signer callback required")
    signer = _falcon_sdk_signer(pk, signer_callback)
    if signer is not None:
        out = _invoke_sdk_signer(signer, unsigned_txn)
        if out is not None:
            return out
    result = signer_callback(unsigned_txn)
    if not isinstance(result, (bytes, bytearray)):
        raise AnchorError("callback must return bytes")
    return bytes(result)


def _falcon_sdk_signer(pk, signer_callback):
    """Falcon1024AlgorandSigner(pk, signer_callback) or TransactionSigner equivalent."""
    if pk is None:
        return None
    for path in (
        ("algosdk.signer", "Falcon1024AlgorandSigner"),
        ("algosdk.signer", "Falcon1024TransactionSigner"),
        ("algosdk.transaction", "Falcon1024AlgorandSigner"),
    ):
        try:
            mod = __import__(path[0], fromlist=[path[1]])
            cls = getattr(mod, path[1])
            return cls(pk, signer_callback)
        except Exception:
            continue
    return None


def _invoke_sdk_signer(signer, unsigned_txn):
    for name in ("sign", "sign_transaction", "sign_txn"):
        fn = getattr(signer, name, None)
        if callable(fn):
            try:
                out = fn(unsigned_txn)
            except Exception:
                return None
            if isinstance(out, (bytes, bytearray)):
                return bytes(out)
    return None


def send_forbidden(*_a, **_k):
    raise RuntimeError("algod send is forbidden in PQ1 construction")


def never_state_proof_covered(status: str) -> str:
    if status == "state_proof_covered":
        raise AnchorError("state_proof_covered is not implemented")
    if status not in ANCHOR_STATUSES:
        raise AnchorError("unknown anchor status")
    return status
