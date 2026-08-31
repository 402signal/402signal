"""Algorand Falcon construction. Default path never broadcasts.

Note 84 bytes = ASCII "402sg/pq1:b" || 0x01 || SHA-256(UTF-8 origin) ||
uint64 BE tree_size || 32-byte RFC 6962 root.

Txn = PaymentTxn amount=0, receiver=sender, flat fee >= 3000 µALGO.
Falcon signing goes through the 6PN client (pq-anchor/1). This module
does not load a Falcon SK and has no Algorand submit function.
send_forbidden() always raises. MainNet genesis is rejected.
"""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Callable

from live402 import algo_tx
from live402.pq import NOTE_FORMAT, NOTE_VERSION
from live402.pq import checkpoint as ckpt
from live402.pq.merkle import HASH_SIZE

NOTE_PREFIX = NOTE_FORMAT.encode("ascii")  # 11 bytes
NOTE_LEN = 84
MIN_FEE = 3000
ANCHOR_STATUSES = frozenset({"pending", "unavailable"})

# TestNet only for any submit path. Do not set MainNet.
TESTNET_NAME = "testnet"
TESTNET_GENESIS_ID = "testnet-v1.0"
MAINNET_GENESIS_ID = "mainnet-v1.0"
TESTNET_GENESIS_HASH = "SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI="
NETWORK_ENV = "LIVE402_PQ_FALCON_NETWORK"
BROADCAST_ENV = "LIVE402_PQ_FALCON_BROADCAST"
ADDRESS_ENV = "LIVE402_PQ_FALCON_ADDRESS"


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


def _field_bytes(val):
    if isinstance(val, (bytes, bytearray)):
        return bytes(val)
    if isinstance(val, str) and val:
        try:
            return bytes.fromhex(val)
        except ValueError:
            return algo_tx.decode_address(val)
    raise AnchorError("not pq1 construction")


# pay_txn fields plus amt (must be 0/absent) and flatFee (construction helper).
# Never close/rekey/lx/grp or any other extra key.
_ALLOWED_INBOUND_KEYS = frozenset({"type", "fee", "fv", "gen", "gh", "lv", "note", "rcv", "snd", "amt", "flatFee"})
_FORBIDDEN_INBOUND_KEYS = frozenset({"close", "rekey", "lx", "grp"})


def validate_unsigned_anchor(txn: dict) -> None:
    """Fail closed unless txn matches PQ1 construction. Does not sign.

    PaymentTxn amount=0, receiver==sender==configured public address,
    genesis testnet-v1.0 (MainNet rejected), note from encode_note.
    Rejects close, rekey, lx, grp, and any unknown key.
    """
    if not isinstance(txn, dict):
        raise AnchorError("not pq1 construction")
    keys = set(txn)
    if keys & _FORBIDDEN_INBOUND_KEYS or not keys.issubset(_ALLOWED_INBOUND_KEYS):
        raise AnchorError("not pq1 construction")
    if str(txn.get("type") or "") != "pay":
        raise AnchorError("not pq1 construction")
    amt = txn.get("amt")
    if amt not in (None, 0):
        raise AnchorError("not pq1 construction")
    gen = str(txn.get("gen") or "")
    if gen == MAINNET_GENESIS_ID or gen != TESTNET_GENESIS_ID:
        raise AnchorError("not pq1 construction")
    addr = falcon_address()
    if not addr:
        raise AnchorError("not pq1 construction")
    try:
        want = algo_tx.decode_address(addr)
        snd = _field_bytes(txn.get("snd"))
        rcv = _field_bytes(txn.get("rcv"))
    except Exception as exc:
        raise AnchorError("not pq1 construction") from exc
    if snd != want or rcv != want:
        raise AnchorError("not pq1 construction")
    try:
        note = txn.get("note")
        if isinstance(note, str):
            note = bytes.fromhex(note)
        decode_note(bytes(note))
    except Exception as exc:
        raise AnchorError("not pq1 construction") from exc


def rebuild_unsigned_anchor(txn: dict) -> dict:
    """Canonical PaymentTxn from allowed fields only. Never copies extra keys."""
    addr = falcon_address()
    if not addr:
        raise AnchorError("not pq1 construction")
    note = txn.get("note")
    if isinstance(note, str):
        note = bytes.fromhex(note)
    note = bytes(note)
    fee = max(int(txn.get("fee") or MIN_FEE), MIN_FEE)
    first = int(txn.get("fv") or 1)
    last = int(txn.get("lv") or (first + 1000))
    gh = txn.get("gh")
    if isinstance(gh, str) and gh.strip():
        try:
            gh = bytes.fromhex(gh)
        except ValueError:
            gh = base64.b64decode(gh)
    elif isinstance(gh, (bytes, bytearray)) and gh:
        gh = bytes(gh)
    else:
        gh = base64.b64decode(TESTNET_GENESIS_HASH)
    rebuilt = algo_tx.pay_txn(addr, addr, 0, fee, first, last, TESTNET_GENESIS_ID, gh, note=note)
    extra = set(rebuilt) - {"type", "fee", "fv", "gen", "gh", "lv", "note", "rcv", "snd"}
    for key in extra:
        rebuilt.pop(key, None)
    return rebuilt


def canonical_unsigned_anchor(txn: dict) -> dict:
    """Validate inbound, then return a rebuilt pay_txn dict. Does not sign."""
    validate_unsigned_anchor(txn)
    return rebuild_unsigned_anchor(txn)


def _genesis_hash_bytes(gen: str, gh):
    if isinstance(gh, (bytes, bytearray)) and gh:
        return bytes(gh)
    if isinstance(gh, str) and gh.strip():
        return base64.b64decode(gh)
    if gen == TESTNET_GENESIS_ID:
        return base64.b64decode(TESTNET_GENESIS_HASH)
    from live402 import algod as algod_mod

    return base64.b64decode(algod_mod.GENESIS_HASH)


def build_payment_txn(sender: str, note: bytes, params: dict | None = None) -> dict:
    """Unsigned PaymentTxn. amount=0, receiver=sender, fee >= 3000. Not submitted."""
    if not sender:
        raise AnchorError("falcon address required")
    p = params if isinstance(params, dict) else {}
    first = int(p.get("firstValid") or p.get("firstRound") or 1)
    last = int(p.get("lastValid") or p.get("lastRound") or (first + 1000))
    gen = str(p.get("genesisID") or p.get("genesis_id") or TESTNET_GENESIS_ID)
    gh = _genesis_hash_bytes(gen, p.get("genesisHash") or p.get("genesis_hash"))
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
    """Default send path. Always raises. No Falcon submit function in this SHA."""
    raise RuntimeError("algod send is forbidden in PQ1 construction")


def never_state_proof_covered(status: str) -> str:
    if status == "state_proof_covered":
        raise AnchorError("state_proof_covered is not implemented")
    if status not in ANCHOR_STATUSES:
        raise AnchorError("unknown anchor status")
    return status


def configured_network() -> str:
    return (os.environ.get(NETWORK_ENV) or "").strip().lower()


def broadcast_requested() -> bool:
    return (os.environ.get(BROADCAST_ENV) or "").strip() == "1"


def falcon_address(sender: str | None = None) -> str:
    raw = (sender or "").strip()
    if raw:
        return raw
    env = (os.environ.get(ADDRESS_ENV) or "").strip()
    if env:
        return env
    from live402.pq import trust

    return trust.falcon_address()


def signer_material_present(signer_callback=None) -> bool:
    """6PN token or injected callback. This app does not hold a Falcon SK."""
    if callable(signer_callback):
        return True
    from live402.pq import signer_client

    return signer_client.token_configured()


def txn_genesis_id(txn: dict | None = None, params: dict | None = None) -> str:
    if isinstance(txn, dict) and txn.get("gen"):
        return str(txn.get("gen"))
    p = params if isinstance(params, dict) else {}
    return str(p.get("genesisID") or p.get("genesis_id") or TESTNET_GENESIS_ID)


def submit_allowed(
    *,
    signer_callback=None,
    sender: str | None = None,
    params: dict | None = None,
    txn: dict | None = None,
) -> bool:
    """True only when every TestNet submit gate is set.

    Requires LIVE402_PQ_FALCON_NETWORK=testnet, BROADCAST=1, a public
    address, 6PN token or callback, and testnet-v1.0 genesis (MainNet
    genesis is rejected). This SHA has no Falcon submit function, so
    this gate never opens a send path.
    """
    gen = txn_genesis_id(txn, params)
    if gen == MAINNET_GENESIS_ID or gen != TESTNET_GENESIS_ID:
        return False
    if configured_network() != TESTNET_NAME:
        return False
    if not broadcast_requested():
        return False
    if not falcon_address(sender):
        return False
    if not signer_material_present(signer_callback):
        return False
    return False
