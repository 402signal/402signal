"""Algorand Falcon construction. Default path never broadcasts.

Note 84 bytes = ASCII "402sg/pq1:b" || 0x01 || SHA-256(UTF-8 origin) ||
uint64 BE tree_size || 32-byte RFC 6962 root.

Txn = PaymentTxn amount=0, receiver=sender. Fee is the official
Falcon required value: max(fee_per_byte * signed_Falcon_txn_size,
3 * protocol_base_min). Protocol base min is 1000 µAlgo today;
Falcon-1024 adds 2x that base (uncongested floor 3000). algod
suggested `fee` is current fee per byte, not a flat txn fee.
Hard ceiling MAX_FEE=30000 µALGO (fail closed if required > cap).
Caller cannot select the fee. Signer and reconstruction derive
the same canonical fee.
Falcon signing goes through the 6PN client (pq-anchor/1). This module
does not load a Falcon SK. send_forbidden() always raises.

TestNet submit of a signer-approved SignedTxn is gated on
LIVE402_PQ_FALCON_BROADCAST=1. That env lives on this 402signal
router (default unset). 402security must GO before anyone sets it
to 1. The isolated signer never reads BROADCAST and never POSTs.
Falcon SK must never live here. Fixture mode and CI never hit live
algod unless a send/fetch hook is injected.

MainNet submit is a separate flag LIVE402_PQ_FALCON_MAINNET_BROADCAST.
A one-shot canary also requires LIVE402_PQ_FALCON_MAINNET_CANARY=1.
Both default unset. TestNet BROADCAST=1 plus NETWORK=mainnet with the
MainNet flag unset never sends. Automatic MainNet broadcast stays off.
send_if_allowed never POSTs MainNet. Worker, tick, and boot never call
submit_mainnet_canary.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlparse

from live402 import algo_tx
from live402.pq import NOTE_FORMAT, NOTE_VERSION
from live402.pq import checkpoint as ckpt
from live402.pq import network as netcfg
from live402.pq.merkle import HASH_SIZE

NOTE_PREFIX = NOTE_FORMAT.encode("ascii")  # 11 bytes
NOTE_LEN = 84
PROTOCOL_BASE_MIN = netcfg.PROTOCOL_BASE_MIN
FALCON_EXTRA_MIN_MULT = netcfg.FALCON_EXTRA_MIN_MULT
MIN_FEE = netcfg.MIN_FEE
MAX_FEE = netcfg.MAX_FEE
FALCON_F1_PK_LEN = netcfg.FALCON_F1_PK_LEN
FALCON_F1_SIG_MAX = netcfg.FALCON_F1_SIG_MAX
ANCHOR_STATUSES = frozenset({"pending", "unavailable"})

TESTNET_NAME = netcfg.TESTNET_NAME
MAINNET_NAME = netcfg.MAINNET_NAME
TESTNET_GENESIS_ID = netcfg.TESTNET_GENESIS_ID
MAINNET_GENESIS_ID = netcfg.MAINNET_GENESIS_ID
TESTNET_GENESIS_HASH = netcfg.TESTNET_GENESIS_HASH
MAINNET_GENESIS_HASH = netcfg.MAINNET_GENESIS_HASH
TESTNET_ALGOD_HOST = netcfg.TESTNET.submit_host
TESTNET_ALGOD_SEND_URL = netcfg.TESTNET.submit_url
TESTNET_ALGOD_PENDING_URL = netcfg.TESTNET.pending_url
TESTNET_INDEXER_HOST = netcfg.TESTNET.confirm_host
TESTNET_INDEXER_TXN_URL = netcfg.TESTNET.confirm_txn_url
TESTNET_EXPLORER_TX_URL = netcfg.TESTNET.explorer_tx_url
MAINNET_ALGOD_HOST = netcfg.MAINNET.submit_host
MAINNET_ALGOD_SEND_URL = netcfg.MAINNET.submit_url
MAINNET_ALGOD_PENDING_URL = netcfg.MAINNET.pending_url
MAINNET_INDEXER_HOST = netcfg.MAINNET.confirm_host
MAINNET_INDEXER_TXN_URL = netcfg.MAINNET.confirm_txn_url
MAINNET_EXPLORER_TX_URL = netcfg.MAINNET.explorer_tx_url
TESTNET_SEND_TIMEOUT = 8.0
TESTNET_FETCH_TIMEOUT = 8.0
USER_AGENT = "402Signal/0.1 (pq falcon testnet; no keys in logs)"
NETWORK_ENV = netcfg.NETWORK_ENV
BROADCAST_ENV = netcfg.BROADCAST_ENV
ADDRESS_ENV = netcfg.ADDRESS_ENV
MAINNET_BROADCAST_ENV = netcfg.MAINNET_BROADCAST_ENV
MAINNET_CANARY_ENV = netcfg.MAINNET_CANARY_ENV
MAINNET_ADDRESS_ENV = netcfg.MAINNET_ADDRESS_ENV
MAINNET_SIGNER_TOKEN_ENV = netcfg.MAINNET_SIGNER_TOKEN_ENV
MAINNET_SIGNER_HOST_ENV = netcfg.MAINNET_SIGNER_HOST_ENV
MAINNET_SIGNER_PORT_ENV = netcfg.MAINNET_SIGNER_PORT_ENV
PQSIG_MARKER = "present"
PQSIG_SCHEME_F1 = "f1"
_EXCLUSIVE_SIG_KEYS = frozenset({"sig", "multisig", "logicsig", "msig", "lsig"})
_TXID_RE = re.compile(r"^[A-Z2-7]{52}$")
_PLACEHOLDER_TXID = frozenset({"", "your_txid", "placeholder", "txid", "none", "null"})


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


def _expected_network_name(expected_network: str | None = None) -> str:
    raw = (expected_network or "").strip().lower()
    if raw:
        if raw not in netcfg.NETWORKS:
            raise AnchorError("unknown network")
        return raw
    from live402.pq import log_identity

    return log_identity.live_network_name()


def _network_cfg(expected_network: str | None = None) -> netcfg.NetworkConfig:
    return netcfg.get_network(_expected_network_name(expected_network))


def protocol_base_min(params: dict | None = None) -> int:
    """algod min-fee: protocol base min for an ordinary txn (1000 today)."""
    p = params if isinstance(params, dict) else {}
    raw = p.get("minFee")
    if raw is None:
        raw = p.get("min-fee")
    if raw is None:
        return PROTOCOL_BASE_MIN
    try:
        n = int(raw)
    except (TypeError, ValueError) as exc:
        raise AnchorError("fee out of range") from exc
    if n < 1:
        raise AnchorError("fee out of range")
    return n


def falcon_min_fee(params: dict | None = None) -> int:
    """Uncongested Falcon-1024 floor: protocol base + 2x base (3000 today)."""
    return protocol_base_min(params) * (1 + FALCON_EXTRA_MIN_MULT)


def fee_per_byte(params: dict | None = None) -> int:
    """algod suggested `fee` is current fee per byte, not a flat txn fee.

    Explicit feePerByte / fee_per_byte / current_fee_per_byte win.
    flatFee=True means a legacy/fixture `fee` field is not per-byte
    (payment-rail suggestedParams). Official algod omits flatFee.
    """
    p = params if isinstance(params, dict) else {}
    for key in ("feePerByte", "fee_per_byte", "current_fee_per_byte"):
        if key in p and p.get(key) is not None:
            raw = p.get(key)
            break
    else:
        if p.get("flatFee") is True:
            return 0
        raw = p.get("fee")
        if raw is None:
            return 0
    try:
        n = int(raw)
    except (TypeError, ValueError) as exc:
        raise AnchorError("fee out of range") from exc
    if n < 0:
        raise AnchorError("fee out of range")
    return n


def estimate_falcon_authorized_size(unsigned: dict | None = None) -> int:
    """Deterministic msgpack size of a Falcon-1024 authorized SignedTxn.

    Uses official max pk (1793) and sig (1423) so router and signer
    derive the same fee when the exact signed blob is not yet known.
    """
    if isinstance(unsigned, dict) and unsigned:
        txn = {k: v for k, v in unsigned.items() if k != "flatFee"}
    else:
        txn = {
            "type": "pay",
            "fee": MIN_FEE,
            "fv": 1,
            "lv": 1001,
            "gen": TESTNET_GENESIS_ID,
            "gh": base64.b64decode(TESTNET_GENESIS_HASH),
            "note": b"\x00" * NOTE_LEN,
            "snd": b"\x00" * 32,
            "rcv": b"\x00" * 32,
        }
    if "fee" not in txn:
        txn = dict(txn)
        txn["fee"] = MIN_FEE
    envelope = {
        "pqsig": {
            "pk": b"\x00" * FALCON_F1_PK_LEN,
            "sch": "f1",
            "sig": b"\x00" * FALCON_F1_SIG_MAX,
            "slt": 0,
        },
        "txn": txn,
    }
    return len(algo_tx.msgpack_encode(envelope))


def required_fee(
    params: dict | None = None,
    *,
    signed: bytes | None = None,
    unsigned: dict | None = None,
) -> int:
    """Official Falcon required fee. Hard ceiling MAX_FEE. No caller fee.

    ONE SIZE RULE: size is ALWAYS the deterministic Falcon-1024
    authorized-envelope estimate (max pk 1793, max compressed sig 1423).
    Never use actual len(signed). If only a SignedTxn is supplied, the
    unsigned txn is decoded so the estimate can include those fields;
    the Falcon sig length is still the official max. Prefer a small
    conservative overpayment over signer/router disagreement.

    required = max(fee_per_byte * estimate, protocol_base_min * 3)
    Fee integers 3000..30000 share the same 3-byte msgpack encoding, so
    the estimate is stable across the allowed fee range.
    """
    floor = falcon_min_fee(params)
    if floor > MAX_FEE:
        raise AnchorError("fee exceeds cap")
    fpb = fee_per_byte(params)
    draft = unsigned
    if draft is None and signed is not None:
        try:
            obj = algo_tx.msgpack_decode(bytes(signed))
            inner = obj.get("txn") if isinstance(obj, dict) else None
            if isinstance(inner, dict):
                draft = inner
        except Exception:
            draft = None
    size = estimate_falcon_authorized_size(draft)
    if size < 1:
        raise AnchorError("fee out of range")
    need = max(fpb * size, floor)
    if need > MAX_FEE:
        raise AnchorError("fee exceeds cap")
    return need


# Algorand MaxTxnLife is 1000 rounds. 402Signal PQ1 policy uses that
# exact reviewed span: lv = fv + 1000. Do not invent a wider window.
MAX_VALIDITY_WINDOW = 1000
CANONICAL_VALIDITY_WINDOW = 1000
# Secondary safety only. Canonical MainNet check is exact fv/lv.
FV_LOOKBACK = 10
FV_LOOKAHEAD = 10
# Frozen suggested-params snapshot must be this fresh at canary POST.
SNAPSHOT_MAX_AGE_S = 90


def snapshot_last_round(params: dict | None = None, *, require: bool = False) -> int:
    """Trusted lastRound from a frozen network-parameter snapshot.

    require=True (MainNet canonical): only lastRound / last_round.
    Missing or invalid fails closed. firstValid is never a lastRound
    substitute on that path.
    """
    p = params if isinstance(params, dict) else {}
    keys = ("lastRound", "last_round") if require else ("lastRound", "last_round")
    for key in keys:
        if p.get(key) not in (None, ""):
            try:
                n = int(p.get(key))
            except (TypeError, ValueError) as exc:
                raise AnchorError("invalid lastRound") from exc
            if n < 1:
                raise AnchorError("invalid lastRound")
            return n
    if require:
        raise AnchorError("missing lastRound")
    return 0


def fee_policy_snapshot(params: dict | None = None, *, unsigned: dict | None = None, now: int | None = None) -> dict:
    """Canonical network-parameter fingerprint bound into AUTHORIZED.

    Values come from the trusted params snapshot only (min fee,
    fee/byte, lastRound). Caller cannot select fee or fv/lv.
    Size for fee is the deterministic Falcon envelope estimate.
    """
    import time as _time

    p = params if isinstance(params, dict) else {}
    require = bool(p.get("require_canonical")) or str(p.get("genesisID") or p.get("genesis_id") or "") == MAINNET_GENESIS_ID
    last_round = snapshot_last_round(p, require=require)
    fv, lv = canonical_validity(p, require_canonical=require)
    canonical = required_fee(p, unsigned=unsigned)
    return {
        "min_fee": protocol_base_min(p),
        "fee_per_byte": fee_per_byte(p),
        "last_round": last_round,
        "max_life": CANONICAL_VALIDITY_WINDOW,
        "max_fee": MAX_FEE,
        "canonical_fee": canonical,
        "fv": fv,
        "lv": lv,
        "snapshot_at": int(now if now is not None else _time.time()),
        "snapshot_max_age_s": SNAPSHOT_MAX_AGE_S,
        "size_rule": "deterministic_falcon_envelope_estimate",
        "formula": "max(fee_per_byte * deterministic_falcon_envelope_estimate, protocol_base_min * 3)",
    }


def params_from_fee_policy(policy: dict | None) -> dict:
    """Rebuild the params snapshot used to derive canonical fee / fv / lv."""
    if not isinstance(policy, dict):
        return {}
    out = {
        "minFee": int(policy.get("min_fee") or PROTOCOL_BASE_MIN),
        "feePerByte": int(policy.get("fee_per_byte") or 0),
        "flatFee": False,
    }
    last_round = int(policy.get("last_round") or 0)
    if last_round:
        out["lastRound"] = last_round
        out["firstValid"] = int(policy.get("fv") or last_round)
        out["lastValid"] = int(policy.get("lv") or (last_round + CANONICAL_VALIDITY_WINDOW))
    return out


def canonical_validity(params: dict | None = None, *, require_canonical: bool = False) -> tuple[int, int]:
    """Canonical firstValid / lastValid from a frozen snapshot.

    MainNet / require_canonical: lastRound is required. fv = lastRound,
    lv = fv + 1000 (MaxTxnLife, reviewed 402Signal policy). No fv=1
    fallback. Missing lastRound fails closed.
    TestNet fixtures without lastRound may use fv=1, lv=1001.
    """
    last_round = snapshot_last_round(params, require=require_canonical)
    if require_canonical and last_round < 1:
        raise AnchorError("missing lastRound")
    if last_round < 1:
        return 1, 1 + CANONICAL_VALIDITY_WINDOW
    return last_round, last_round + CANONICAL_VALIDITY_WINDOW


def validate_validity_window(
    fv: int,
    lv: int,
    params: dict | None = None,
    *,
    require_canonical: bool = False,
) -> None:
    """Bound firstValid/lastValid. Fail closed on broad or stale windows.

    MainNet canonical: actual fv == policy.fv AND actual lv == policy.lv.
    The ±10 lookback/lookahead is a secondary safety bound only.
    Span > MaxTxnLife is always rejected. Already-expired (lv < lastRound)
    is rejected.
    """
    try:
        first = int(fv)
        last = int(lv)
    except (TypeError, ValueError) as exc:
        raise AnchorError("invalid validity window") from exc
    if first < 1 or last < 1:
        raise AnchorError("invalid validity window")
    span = last - first
    if span < 1 or span > MAX_VALIDITY_WINDOW:
        raise AnchorError("validity window out of range")
    if require_canonical:
        want_fv, want_lv = canonical_validity(params, require_canonical=True)
        if first != want_fv or last != want_lv:
            raise AnchorError("validity window not canonical")
        last_round = snapshot_last_round(params, require=True)
        if first < last_round - FV_LOOKBACK or first > last_round + FV_LOOKAHEAD:
            raise AnchorError("firstValid stale or too far ahead")
        if last < last_round:
            raise AnchorError("lastValid already expired")
        return
    last_round = snapshot_last_round(params, require=False)
    if last_round >= 1:
        if first < last_round - FV_LOOKBACK or first > last_round + FV_LOOKAHEAD:
            raise AnchorError("firstValid stale or too far ahead")
        if last < last_round:
            raise AnchorError("lastValid already expired")


def snapshot_fresh(policy: dict | None, *, now: int | None = None) -> None:
    """Fail closed if the frozen suggested-params snapshot is too old."""
    import time as _time

    if not isinstance(policy, dict):
        raise AnchorError("stale snapshot")
    at = int(policy.get("snapshot_at") or 0)
    max_age = int(policy.get("snapshot_max_age_s") or SNAPSHOT_MAX_AGE_S)
    if at < 1:
        raise AnchorError("stale snapshot")
    when = int(now if now is not None else _time.time())
    if when - at > max_age:
        raise AnchorError("stale snapshot")


def signed_txn_txid(signed: bytes) -> str:
    """Local expected Algorand txid from the exact authorized SignedTxn."""
    try:
        txid = algo_tx.txid_from_signed(bytes(signed))
    except Exception as exc:
        raise AnchorError("invalid signed txn txid") from exc
    if not _looks_like_txid(txid):
        raise AnchorError("invalid signed txn txid")
    return txid


def validate_unsigned_anchor(txn: dict, expected_network: str | None = None) -> None:
    """Fail closed unless txn matches PQ1 construction. Does not sign.

    PaymentTxn amount=0, receiver==sender==configured public address,
    genesis must exactly match expected_network (default TestNet live path).
    MainNet genesis is rejected unless expected_network is mainnet.
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
    cfg = _network_cfg(expected_network)
    gen = str(txn.get("gen") or "")
    if gen != cfg.genesis_id:
        raise AnchorError("not pq1 construction")
    addr = falcon_address_for(cfg.name)
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


def rebuild_unsigned_anchor(
    txn: dict,
    expected_network: str | None = None,
    params: dict | None = None,
) -> dict:
    """Canonical PaymentTxn from allowed fields only. Never copies extra keys.

    Fee is derived from suggested params + Falcon signed size. Inbound
    txn.fee is ignored. Caller cannot select the fee.
    """
    cfg = _network_cfg(expected_network)
    addr = falcon_address_for(cfg.name)
    if not addr:
        raise AnchorError("not pq1 construction")
    note = txn.get("note")
    if isinstance(note, str):
        note = bytes.fromhex(note)
    note = bytes(note)
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
        gh = base64.b64decode(cfg.genesis_hash)
    if base64.b64encode(bytes(gh)).decode("ascii") != cfg.genesis_hash:
        raise AnchorError("genesis hash mismatch")
    draft = algo_tx.pay_txn(addr, addr, 0, falcon_min_fee(params), first, last, cfg.genesis_id, gh, note=note)
    fee = required_fee(params, unsigned=draft)
    rebuilt = algo_tx.pay_txn(addr, addr, 0, fee, first, last, cfg.genesis_id, gh, note=note)
    extra = set(rebuilt) - {"type", "fee", "fv", "gen", "gh", "lv", "note", "rcv", "snd"}
    for key in extra:
        rebuilt.pop(key, None)
    return rebuilt


def canonical_unsigned_anchor(
    txn: dict,
    expected_network: str | None = None,
    params: dict | None = None,
) -> dict:
    """Validate inbound, then return a rebuilt pay_txn dict. Does not sign."""
    validate_unsigned_anchor(txn, expected_network=expected_network)
    return rebuild_unsigned_anchor(txn, expected_network=expected_network, params=params)


def _genesis_hash_bytes(gen: str, gh):
    if isinstance(gh, (bytes, bytearray)) and gh:
        return bytes(gh)
    if isinstance(gh, str) and gh.strip():
        return base64.b64decode(gh)
    cfg = netcfg.network_for_genesis_id(gen)
    if cfg is not None:
        return base64.b64decode(cfg.genesis_hash)
    if gen == TESTNET_GENESIS_ID:
        return base64.b64decode(TESTNET_GENESIS_HASH)
    from live402 import algod as algod_mod

    return base64.b64decode(algod_mod.GENESIS_HASH)


def build_payment_txn(sender: str, note: bytes, params: dict | None = None) -> dict:
    """Unsigned PaymentTxn. amount=0, receiver=sender. Not submitted.

    Fee is derived: max(fee_per_byte * Falcon signed size, falcon_min).
    Caller cannot select the fee. If required > MAX_FEE the call fails closed.
    """
    if not sender:
        raise AnchorError("falcon address required")
    p = params if isinstance(params, dict) else {}
    first = int(p.get("firstValid") or p.get("firstRound") or 1)
    last = int(p.get("lastValid") or p.get("lastRound") or (first + 1000))
    gen = str(p.get("genesisID") or p.get("genesis_id") or TESTNET_GENESIS_ID)
    gh = _genesis_hash_bytes(gen, p.get("genesisHash") or p.get("genesis_hash"))
    draft = algo_tx.pay_txn(sender, sender, 0, falcon_min_fee(p), first, last, gen, gh, note=note)
    fee = required_fee(p, unsigned=draft)
    txn = algo_tx.pay_txn(sender, sender, 0, fee, first, last, gen, gh, note=note)
    txn["flatFee"] = True
    return txn


def build_mainnet_payment_txn(note: bytes, params: dict | None = None, *, address: str | None = None) -> dict:
    """MainNet PaymentTxn from trusted semantic fields only. Never submitted here.

    amount 0, sender=receiver=configured MainNet Falcon f1. Genesis is the
    exact MainNet ID+hash. Extra inbound keys are not copied. Fee is
    derived; caller cannot select it.
    """
    addr = (address or falcon_address_for(MAINNET_NAME) or "").strip()
    if not addr:
        raise AnchorError("mainnet falcon address required")
    p = dict(params) if isinstance(params, dict) else {}
    gen = str(p.get("genesisID") or p.get("genesis_id") or MAINNET_GENESIS_ID)
    if gen != MAINNET_GENESIS_ID:
        raise AnchorError("not mainnet genesis")
    gh = _genesis_hash_bytes(gen, p.get("genesisHash") or p.get("genesis_hash"))
    if base64.b64encode(bytes(gh)).decode("ascii") != MAINNET_GENESIS_HASH:
        raise AnchorError("genesis hash mismatch")
    if falcon_min_fee(p) > MAX_FEE:
        raise AnchorError("fee exceeds cap")
    first, last = canonical_validity(p, require_canonical=True)
    draft = algo_tx.pay_txn(addr, addr, 0, falcon_min_fee(p), first, last, MAINNET_GENESIS_ID, gh, note=bytes(note))
    fee = required_fee(p, unsigned=draft)
    txn = algo_tx.pay_txn(addr, addr, 0, fee, first, last, MAINNET_GENESIS_ID, gh, note=bytes(note))
    extra = set(txn) - {"type", "fee", "fv", "gen", "gh", "lv", "note", "rcv", "snd"}
    for key in extra:
        txn.pop(key, None)
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
    """Default send path. Always raises. Broadcast uses send_if_allowed."""
    raise RuntimeError("algod send is forbidden in PQ1 construction")


def never_state_proof_covered(status: str) -> str:
    if status == "state_proof_covered":
        raise AnchorError("state_proof_covered is not implemented")
    if status not in ANCHOR_STATUSES:
        raise AnchorError("unknown anchor status")
    return status


def configured_network() -> str:
    from live402.pq import log_identity

    return log_identity.configured_network()


def broadcast_requested() -> bool:
    return (os.environ.get(BROADCAST_ENV) or "").strip() == "1"


def mainnet_broadcast_requested() -> bool:
    return (os.environ.get(MAINNET_BROADCAST_ENV) or "").strip() == "1"


def mainnet_canary_requested() -> bool:
    """One-shot human canary gate. Default unset. Worker never reads this."""
    return (os.environ.get(MAINNET_CANARY_ENV) or "").strip() == "1"


def automatic_mainnet_enabled() -> bool:
    """Later GO. Hard-off in this PR. Worker and tick never MainNet-submit."""
    return False


def falcon_address(sender: str | None = None) -> str:
    raw = (sender or "").strip()
    if raw:
        return raw
    env = (os.environ.get(ADDRESS_ENV) or "").strip()
    if env:
        return env
    from live402.pq import trust

    return trust.falcon_address()


def falcon_address_for(network: str, sender: str | None = None) -> str:
    raw = (sender or "").strip()
    if raw:
        return raw
    name = (network or "").strip().lower()
    if name == MAINNET_NAME:
        return (os.environ.get(MAINNET_ADDRESS_ENV) or "").strip()
    return falcon_address()


def mainnet_signer_configured() -> bool:
    """Explicit MainNet signer HMAC token. Named, never valued here."""
    return bool((os.environ.get(MAINNET_SIGNER_TOKEN_ENV) or "").strip())


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


def _params_genesis_hash(params: dict | None, txn: dict | None = None) -> str:
    raw = None
    if isinstance(txn, dict):
        raw = txn.get("gh")
    if raw in (None, "") and isinstance(params, dict):
        raw = params.get("genesisHash") or params.get("genesis_hash")
    if raw in (None, ""):
        gen = txn_genesis_id(txn, params)
        cfg = netcfg.network_for_genesis_id(gen)
        return cfg.genesis_hash if cfg is not None else ""
    if isinstance(raw, (bytes, bytearray)):
        return base64.b64encode(bytes(raw)).decode("ascii")
    text = str(raw).strip()
    if not text:
        return ""
    try:
        return base64.b64encode(base64.b64decode(text)).decode("ascii")
    except Exception:
        try:
            return base64.b64encode(bytes.fromhex(text)).decode("ascii")
        except ValueError:
            return ""


def submit_allowed(
    *,
    signer_callback=None,
    sender: str | None = None,
    params: dict | None = None,
    txn: dict | None = None,
) -> bool:
    """True only when every TestNet submit gate is set.

    Requires LIVE402_PQ_FALCON_NETWORK=testnet, router BROADCAST=1, a
    public address, and testnet-v1.0 genesis (MainNet genesis is
    rejected on this path). NETWORK=mainnet never returns True here.
    LIVE402_PQ_FALCON_BROADCAST=1 plus NETWORK=mainnet plus an unset
    MainNet flag never sends. Signer never reads BROADCAST.
    """
    if configured_network() != TESTNET_NAME:
        return False
    gen = txn_genesis_id(txn, params)
    if gen != TESTNET_GENESIS_ID:
        return False
    gh = _params_genesis_hash(params, txn)
    if gh and gh != TESTNET_GENESIS_HASH:
        return False
    if not broadcast_requested():
        return False
    if not falcon_address(sender):
        return False
    return True


def mainnet_submit_allowed(
    *,
    sender: str | None = None,
    params: dict | None = None,
    txn: dict | None = None,
    signed: bytes | None = None,
    expected_origin: str = "",
    expected_size: int = 0,
    expected_root=None,
    allow_fixture_send_hook: bool = False,
) -> bool:
    """True only when every MainNet submit gate is set. Fail closed.

    Requires ALL of: NETWORK=mainnet, exact MainNet genesis ID+hash,
    configured MainNet Falcon address, explicit MainNet signer token,
    valid checkpoint fields when a SignedTxn is supplied, semantic
    SignedTxn OK, fee within cap, allowlisted MainNet submit host,
    and LIVE402_PQ_FALCON_MAINNET_BROADCAST=1.
    TestNet BROADCAST=1 does not satisfy the MainNet flag.
    Fixture/CI is refused unless allow_fixture_send_hook (tests only).
    The one-shot canary env is a separate gate on submit_mainnet_canary.
    """
    from live402 import fixtures

    if configured_network() != MAINNET_NAME:
        return False
    if not mainnet_broadcast_requested():
        return False
    if fixtures.fixture_mode() and not allow_fixture_send_hook:
        return False
    from live402.pq import log_identity
    from live402.pq import store as pq_store

    try:
        log_identity.require_mainnet_identity(
            db_path=pq_store.db_path(),
            origin=log_identity.configured_origin(),
        )
    except log_identity.ConfigError:
        return False
    gen = txn_genesis_id(txn, params)
    if gen != MAINNET_GENESIS_ID:
        return False
    gh = _params_genesis_hash(params, txn)
    if gh != MAINNET_GENESIS_HASH:
        return False
    addr = falcon_address_for(MAINNET_NAME, sender)
    if not addr:
        return False
    if not mainnet_signer_configured():
        return False
    if not netcfg.submit_host_allowlisted(MAINNET_NAME, MAINNET_ALGOD_HOST):
        return False
    if not _pinned_https(MAINNET_ALGOD_SEND_URL, MAINNET_ALGOD_HOST):
        return False
    if signed is not None:
        if not expected_origin or int(expected_size or 0) < 1 or expected_root in (None, "", b""):
            return False
        try:
            validate_signed_txn(
                bytes(signed),
                expected_origin=expected_origin,
                expected_size=int(expected_size),
                expected_root=expected_root,
                expected_address=addr,
                expected_network=MAINNET_NAME,
                params=params,
                require_canonical=True,
            )
        except AnchorError:
            return False
    return True


def _looks_like_txid(txid: str) -> bool:
    text = (txid or "").strip()
    low = text.lower()
    if low in _PLACEHOLDER_TXID or "placeholder" in low or text == "YOUR_TXID":
        return False
    return bool(_TXID_RE.match(text))


def testnet_explorer_url(txid: str) -> str:
    if not _looks_like_txid(txid):
        raise AnchorError("invalid confirmed fields")
    return TESTNET_EXPLORER_TX_URL + txid.strip()


def explorer_url(txid: str, network: str | None = None) -> str:
    if not _looks_like_txid(txid):
        raise AnchorError("invalid confirmed fields")
    cfg = _network_cfg(network)
    return cfg.explorer_tx_url + txid.strip()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _pinned_https(url: str, host: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != host:
        return False
    if parsed.username or parsed.password:
        return False
    return True


_FORBIDDEN_SIGNED_KEYS = frozenset(
    {"sgnr", "sig", "msig", "lsig", "lsg", "auth-addr", "authAddr", "auth_addr"}
)
_FORBIDDEN_TXN_TYPES = frozenset({"axfer", "appl", "acfg", "afrz", "keyreg", "stpf"})


def validate_signed_txn(
    signed: bytes,
    *,
    expected_origin: str,
    expected_size: int,
    expected_root,
    expected_address: str | None = None,
    expected_network: str | None = None,
    params: dict | None = None,
    require_canonical: bool | None = None,
) -> dict:
    """Semantic verify of a SignedTxn before any broadcast.

    Pay-0 self-Falcon, expected note/origin/size/root, fee cap, exact
    genesis for expected_network, no AuthAddr/rekey/close/group/lease/
    axfer/appcall/LogicSig/multisig/ordinary sig. Does not parse a
    Falcon secret key. Does not POST.

    MainNet (or require_canonical=True) requires fee == canonical
    required_fee(params) and a bounded fv/lv window. TestNet keeps
    the range check unless require_canonical is set.
    """
    if not isinstance(signed, (bytes, bytearray)) or not signed:
        raise AnchorError("not a signed pq1 txn")
    blob = bytes(signed)
    if blob == PQSIG_MARKER.encode("utf-8"):
        raise AnchorError("pqsig marker is not a signed txn")
    try:
        obj = algo_tx.msgpack_decode(blob)
    except Exception as exc:
        raise AnchorError("not a signed pq1 txn") from exc
    if not isinstance(obj, dict):
        raise AnchorError("not a signed pq1 txn")
    for key in _FORBIDDEN_SIGNED_KEYS:
        if key in obj and _nonzero_blob(obj.get(key)):
            raise AnchorError("auth address forbidden")
    txn = obj.get("txn")
    if not isinstance(txn, dict):
        raise AnchorError("not a signed pq1 txn")
    tx_type = str(txn.get("type") or "")
    if tx_type in _FORBIDDEN_TXN_TYPES or tx_type != "pay":
        raise AnchorError("not a payment")
    if any(k in txn for k in ("close", "rekey", "lx", "grp", "aamt", "xaid", "apid", "arcv")):
        raise AnchorError("not pq1 construction")
    if _nonzero_blob(txn.get("close")) or _nonzero_blob(txn.get("rekey")):
        raise AnchorError("rekey/close forbidden")
    if _nonzero_blob(txn.get("grp")) or _nonzero_blob(txn.get("lx")):
        raise AnchorError("group/lease forbidden")
    amt = txn.get("amt")
    if amt not in (None, 0):
        raise AnchorError("amount must be 0")
    cfg = _network_cfg(expected_network)
    gen = str(txn.get("gen") or "")
    if gen != cfg.genesis_id:
        raise AnchorError("genesis mismatch")
    gh_raw = txn.get("gh")
    if gh_raw not in (None, "", b""):
        if isinstance(gh_raw, (bytes, bytearray)):
            have_gh = base64.b64encode(bytes(gh_raw)).decode("ascii")
        else:
            have_gh = _params_genesis_hash(None, txn)
        if have_gh and have_gh != cfg.genesis_hash:
            raise AnchorError("genesis hash mismatch")
    fee = int(txn.get("fee") or 0)
    canonical = require_canonical
    if canonical is None:
        canonical = cfg.name == MAINNET_NAME
    if canonical:
        # Fee is derived from the deterministic Falcon envelope estimate,
        # the same algorithm the signer used before the exact blob existed.
        want_fee = required_fee(params, unsigned=txn)
        if fee != want_fee:
            raise AnchorError("fee not canonical")
        validate_validity_window(
            int(txn.get("fv") or 0),
            int(txn.get("lv") or 0),
            params,
            require_canonical=True,
        )
    else:
        if fee < falcon_min_fee(params) or fee > MAX_FEE:
            raise AnchorError("fee out of range")
        try:
            validate_validity_window(
                int(txn.get("fv") or 0),
                int(txn.get("lv") or 0),
                params,
                require_canonical=False,
            )
        except AnchorError:
            # TestNet fixtures may omit lastRound; still reject broad windows.
            span = int(txn.get("lv") or 0) - int(txn.get("fv") or 0)
            if span < 1 or span > MAX_VALIDITY_WINDOW:
                raise
    addr = (expected_address or falcon_address_for(cfg.name) or "").strip()
    if not addr:
        raise AnchorError("falcon address required")
    try:
        want = algo_tx.decode_address(addr)
        snd = _field_bytes(txn.get("snd"))
        rcv = _field_bytes(txn.get("rcv"))
    except Exception as exc:
        raise AnchorError("sender/receiver mismatch") from exc
    if snd != want or rcv != want:
        raise AnchorError("sender/receiver mismatch")
    try:
        note = txn.get("note")
        if isinstance(note, str):
            note = bytes.fromhex(note)
        parsed = decode_note(bytes(note))
    except Exception as exc:
        raise AnchorError("invalid note") from exc
    if parsed["origin_hash"] != origin_hash(expected_origin):
        raise AnchorError("origin mismatch")
    if int(parsed["tree_size"]) != int(expected_size):
        raise AnchorError("tree size mismatch")
    if isinstance(expected_root, (bytes, bytearray)):
        want_root = bytes(expected_root)
    else:
        try:
            want_root = bytes.fromhex(str(expected_root or ""))
        except ValueError as exc:
            raise AnchorError("invalid root") from exc
    if parsed["root"] != want_root:
        raise AnchorError("root mismatch")
    pq = _pq_auth_from_obj(obj)
    if not pq:
        raise AnchorError("falcon authorization missing")
    expected_txid = signed_txn_txid(blob)
    return {
        "origin": expected_origin,
        "tree_size": int(parsed["tree_size"]),
        "root": parsed["root"],
        "address": addr,
        "fee": fee,
        "fv": int(txn.get("fv") or 0),
        "lv": int(txn.get("lv") or 0),
        "txid": expected_txid,
        "network": cfg.name,
        "genesis_id": cfg.genesis_id,
    }


def send_if_allowed(signed: bytes, *, send_fn=None, sender: str | None = None, params: dict | None = None) -> str | None:
    """POST signer-approved SignedTxn bytes only when TestNet submit_allowed.

    Never posts MainNet. MainNet uses submit_mainnet_canary only.
    send_fn is injected by tests. Fixture mode without send_fn never
    dials live algod. The pqsig marker is never treated as txn bytes.
    """
    if not isinstance(signed, (bytes, bytearray)) or not signed:
        return None
    blob = bytes(signed)
    if blob == PQSIG_MARKER.encode("utf-8") or blob == PQSIG_MARKER.encode("ascii"):
        return None
    if configured_network() == MAINNET_NAME:
        return None
    if not submit_allowed(sender=sender, params=params):
        return None
    if send_fn is not None:
        if not callable(send_fn):
            return None
        out = send_fn(blob)
        if out is None:
            return None
        text = str(out).strip()
        return text if _looks_like_txid(text) else None
    from live402 import fixtures

    if fixtures.fixture_mode():
        return None
    return _post_testnet(blob)


def submit_provider(network: str) -> dict:
    """Allowlisted algod submit target. Separate from confirm_provider."""
    cfg = netcfg.get_network(network)
    return {
        "network": cfg.name,
        "kind": "submit",
        "host": cfg.submit_host,
        "url": cfg.submit_url,
        "org": netcfg.provider_org(cfg.submit_host),
        "allowlisted": netcfg.submit_host_allowlisted(cfg.name, cfg.submit_host),
    }


def confirm_provider(network: str) -> dict:
    """Allowlisted fetch+decode target. Separate from submit_provider.

    Separable endpoints are not the same as independent confirmation.
    Independence is true only when submit and confirm map to known,
    different organizations. Default MainNet hosts are both Nodely
    (AlgoNode is the legacy brand). That does not satisfy the
    production independent-confirmation requirement.
    LIVE402_PQ_CONFIRM_PROVIDER selects a hardcoded table entry
    (tatum | nownodes). Default trust_root.v2 stays
    independent_provider=false. confirmation_ready is a separate
    runtime flag and does not rewrite the committed trust root.
    """
    cfg = netcfg.get_network(network)
    submit = submit_provider(cfg.name)
    try:
        host = netcfg.configured_confirm_host(cfg.name)
        url = netcfg.configured_confirm_txn_url(cfg.name)
        status = netcfg.confirmation_status(cfg.name)
    except netcfg.UnknownNetwork:
        host = cfg.confirm_host
        url = cfg.confirm_txn_url
        status = {
            "confirm_provider_known": False,
            "confirm_org_independent": False,
            "confirm_credentials_configured": False,
            "confirm_reachable": False,
            "confirm_falcon_compatible": False,
            "confirmation_ready": False,
        }
    independent = netcfg.confirmation_independent(cfg.submit_host, host)
    out = {
        "network": cfg.name,
        "kind": "confirm",
        "host": host,
        "url": url,
        "pending_url": cfg.pending_url,
        "org": netcfg.provider_org(host),
        "submit_org": netcfg.provider_org(cfg.submit_host),
        "second_host_allowlisted": netcfg.NODELY_MAINNET_CONFIRM_HOST if cfg.name == MAINNET_NAME else "",
        "allowlisted": netcfg.confirm_host_allowlisted(cfg.name, host),
        "independent_of_submit": independent,
        "independence_status": (
            "met_different_org" if independent else netcfg.CONFIRM_INDEPENDENCE_STATUS
        ),
        "independence_requirement": netcfg.CONFIRM_INDEPENDENCE_REQUIREMENT,
        "submit_host": submit.get("host") or "",
        "confirm_provider_known": bool(status.get("confirm_provider_known")),
        "confirm_org_independent": bool(status.get("confirm_org_independent")),
        "confirm_credentials_configured": bool(status.get("confirm_credentials_configured")),
        "confirm_reachable": bool(status.get("confirm_reachable")),
        "confirm_falcon_compatible": bool(status.get("confirm_falcon_compatible")),
        "confirmation_ready": bool(status.get("confirmation_ready")),
    }
    return out


def submit_mainnet_canary(
    signed: bytes,
    *,
    authorize_human_canary: bool,
    sender: str | None = None,
    params: dict | None = None,
    expected_origin: str = "",
    expected_size: int = 0,
    expected_root=None,
    send_fn=None,
) -> str | None:
    """Human one-shot MainNet canary. Worker, tick, and boot never call this.

    Requires authorize_human_canary is True, automatic MainNet still off,
    LIVE402_PQ_FALCON_MAINNET_BROADCAST=1, LIVE402_PQ_FALCON_MAINNET_CANARY=1,
    and every mainnet_submit_allowed gate. Both envs default unset.
    Tests inject send_fn to exercise authorize -> validate -> would POST.
    Fixture mode never dials live algod. Unset MAINNET_BROADCAST is the
    kill switch. Destroying the Falcon key is not the kill switch.
    """
    if authorize_human_canary is not True:
        raise AnchorError("canary not authorized")
    if automatic_mainnet_enabled():
        raise AnchorError("automatic mainnet is a later GO")
    if not mainnet_canary_requested():
        raise AnchorError("canary gate off")
    if not mainnet_broadcast_requested():
        raise AnchorError("mainnet broadcast gate off")
    blob = bytes(signed) if isinstance(signed, (bytes, bytearray)) else b""
    if not blob or blob == PQSIG_MARKER.encode("utf-8"):
        raise AnchorError("not a signed pq1 txn")
    hook = send_fn is not None
    if not mainnet_submit_allowed(
        sender=sender,
        params=params,
        signed=blob,
        expected_origin=expected_origin,
        expected_size=expected_size,
        expected_root=expected_root,
        allow_fixture_send_hook=hook,
    ):
        raise AnchorError("mainnet submit gates failed")
    if send_fn is not None:
        if not callable(send_fn):
            raise AnchorError("invalid send hook")
        out = send_fn(blob)
        text = str(out or "").strip()
        if not _looks_like_txid(text):
            raise AnchorError("invalid confirmed fields")
        return text
    from live402 import fixtures

    if fixtures.fixture_mode():
        raise AnchorError("fixture mode never sends mainnet")
    return _post_mainnet(blob)


def _post_mainnet(blob: bytes) -> str | None:
    """POST SignedTxn bytes to pinned MainNet algod. Dual-gate only.

    Reached only after submit_mainnet_canary has checked both
    MAINNET_BROADCAST and MAINNET_CANARY, plus every identity gate.
    Worker and boot never call this. Fixture mode never reaches this.
    """
    if not mainnet_broadcast_requested() or not mainnet_canary_requested():
        return None
    if automatic_mainnet_enabled():
        return None
    if not _pinned_https(MAINNET_ALGOD_SEND_URL, MAINNET_ALGOD_HOST):
        return None
    if not netcfg.submit_host_allowlisted(MAINNET_NAME, MAINNET_ALGOD_HOST):
        return None
    req = urllib.request.Request(
        MAINNET_ALGOD_SEND_URL,
        data=bytes(blob),
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/x-binary",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=TESTNET_SEND_TIMEOUT) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if not isinstance(status, int) or status < 200 or status >= 300:
                return None
            raw = resp.read(512)
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    txid = str(body.get("txId") or body.get("txid") or "").strip()
    return txid if _looks_like_txid(txid) else None


def _post_testnet(blob: bytes) -> str | None:
    """POST SignedTxn bytes to pinned TestNet algod. Never MainNet."""
    if not _pinned_https(TESTNET_ALGOD_SEND_URL, TESTNET_ALGOD_HOST):
        return None
    req = urllib.request.Request(
        TESTNET_ALGOD_SEND_URL,
        data=bytes(blob),
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/x-binary",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=TESTNET_SEND_TIMEOUT) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if not isinstance(status, int) or status < 200 or status >= 300:
                return None
            raw = resp.read(512)
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    txid = str(body.get("txId") or body.get("txid") or "").strip()
    return txid if _looks_like_txid(txid) else None


def _get_pinned(url: str, host: str, timeout: float, extra_headers: dict | None = None) -> bytes | None:
    if not _pinned_https(url, host):
        return None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if extra_headers:
        for key, val in extra_headers.items():
            if key and val:
                headers[str(key)] = str(val)
    req = urllib.request.Request(
        url,
        method="GET",
        headers=headers,
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if not isinstance(status, int) or status < 200 or status >= 300:
                return None
            return resp.read(65536)
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None


def fetch_confirmed_txn(txid: str, network: str | None = None, fetch_fn=None):
    """Independently GET a confirmed txn and return the JSON object.

    Confirmation is fetch+decode, not HTTP 200 or a txid string.
    fetch_fn is injected by tests. Fixture mode without fetch_fn never
    dials live indexer/algod. Host must be the allowlisted confirm provider.
    """
    if not _looks_like_txid(txid):
        return None
    if fetch_fn is not None:
        if not callable(fetch_fn):
            return None
        return fetch_fn(txid)
    from live402 import fixtures

    if fixtures.fixture_mode():
        return None
    cfg = _network_cfg(network)
    try:
        confirm_host = netcfg.configured_confirm_host(cfg.name)
        idx_url = netcfg.configured_confirm_txn_url(cfg.name, txid)
        auth = netcfg.confirm_auth_header() if cfg.name == MAINNET_NAME else None
    except netcfg.UnknownNetwork:
        return None
    if not netcfg.confirm_host_allowlisted(cfg.name, confirm_host):
        return None
    extra = {auth[0]: auth[1]} if auth else None
    raw = _get_pinned(idx_url, confirm_host, TESTNET_FETCH_TIMEOUT, extra_headers=extra)
    if raw:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, dict):
            return data
    # MainNet pending on the submit host is not independent confirmation.
    if cfg.name == MAINNET_NAME:
        return None
    pending_url = cfg.pending_url + txid
    pending_host = urlparse(cfg.pending_url).hostname or cfg.submit_host
    if not netcfg.pending_host_allowlisted(cfg.name, pending_host):
        return None
    raw = _get_pinned(pending_url, pending_host, TESTNET_FETCH_TIMEOUT)
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def fetch_testnet_txn(txid: str, fetch_fn=None):
    """Independently GET a confirmed TestNet txn. Never trusts caller fields.

    fetch_fn is injected by tests. Fixture mode without fetch_fn never
    dials live indexer/algod. Default confirm provider is TestNet.
    """
    if not _looks_like_txid(txid):
        return None
    if fetch_fn is not None:
        if not callable(fetch_fn):
            return None
        return fetch_fn(txid)
    from live402 import fixtures

    if fixtures.fixture_mode():
        return None
    idx_url = TESTNET_INDEXER_TXN_URL + txid
    raw = _get_pinned(idx_url, TESTNET_INDEXER_HOST, TESTNET_FETCH_TIMEOUT)
    if raw:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, dict):
            return data
    pending_url = TESTNET_ALGOD_PENDING_URL + txid
    raw = _get_pinned(pending_url, TESTNET_ALGOD_HOST, TESTNET_FETCH_TIMEOUT)
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _b64(val):
    if val is None or val == "":
        return b""
    if isinstance(val, (bytes, bytearray)):
        return bytes(val)
    text = str(val).strip()
    if not text:
        return b""
    try:
        return base64.b64decode(text)
    except Exception:
        try:
            return bytes.fromhex(text)
        except ValueError:
            return text.encode("utf-8")


def _addr_text(val) -> str:
    if val is None or val == "":
        return ""
    if isinstance(val, (bytes, bytearray)):
        if len(val) == 32:
            try:
                return algo_tx.encode_address(bytes(val))
            except ValueError:
                return ""
        return ""
    text = str(val).strip()
    if len(text) == 58:
        try:
            algo_tx.decode_address(text)
            return text
        except ValueError:
            return ""
    try:
        raw = _b64(text)
        if len(raw) == 32:
            return algo_tx.encode_address(raw)
    except Exception:
        return ""
    return ""


def _nonzero_blob(val) -> bool:
    if val is None or val == "" or val == 0:
        return False
    if isinstance(val, (bytes, bytearray)):
        return any(val)
    if isinstance(val, str):
        return bool(val.strip())
    return True


def _field_bytes_or_empty(val) -> bytes:
    if val is None or val == "":
        return b""
    if isinstance(val, (bytes, bytearray)):
        return bytes(val)
    return _b64(val)


def _parse_pqsig_envelope(raw):
    """Official Algorand pqsig envelope. Codec {sch,slt,pk,sig} or indexer REST.

    sch/scheme must be exactly f1 (Falcon-1024). slt/salt if present is
    0-255. pk/public-key and sig/signature must be non-empty bytes.
    Fail closed on missing, empty, f5, or any other scheme. A bare
    blob (including signature.falcon) is not an envelope.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("pqsig") == PQSIG_MARKER or raw == PQSIG_MARKER:
        return None
    sch = raw.get("sch") if "sch" in raw else raw.get("scheme")
    if sch is None:
        return None
    scheme = str(sch).strip()
    if scheme != PQSIG_SCHEME_F1:
        return None
    slt = raw.get("slt") if "slt" in raw else raw.get("salt")
    if slt is not None and slt != "":
        try:
            salt = int(slt)
        except (TypeError, ValueError):
            return None
        if salt < 0 or salt > 255:
            return None
    pk = _field_bytes_or_empty(raw.get("pk") if "pk" in raw else raw.get("public-key"))
    sig = _field_bytes_or_empty(raw.get("sig") if "sig" in raw else raw.get("signature"))
    if not pk or not sig:
        return None
    if pk == PQSIG_MARKER.encode("utf-8") or sig == PQSIG_MARKER.encode("utf-8"):
        return None
    return bytes(sig)


def _sig_type_value(obj: dict) -> str:
    for key in ("sig-type", "sigType", "signature-type"):
        if key in obj and obj.get(key) is not None and obj.get(key) != "":
            return str(obj.get(key)).strip()
    return ""


def _exclusive_sig_present(sig: dict) -> bool:
    for key in _EXCLUSIVE_SIG_KEYS:
        if key not in sig:
            continue
        val = sig.get(key)
        if val in (None, "", {}, []):
            continue
        if isinstance(val, (bytes, bytearray)) and not val:
            continue
        return True
    return False


def _pq_auth_from_obj(obj: dict):
    """Positive PQ/Falcon authorization from an official pqsig envelope.

    Accepts consensus SignedTxn codec tags (pqsig.{sch,slt,pk,sig}) and
    indexer REST TransactionSignaturePQsig (signature.pqsig with
    scheme/salt/public-key/signature). sch/scheme must be f1.

    Fail closed: signature.falcon blobs, Ed25519 signature.sig, missing
    pqsig, empty/other scheme (including f5), missing pk/sig, the 6PN
    marker pqsig="present", and StateProof falcon-signature. Confirmed
    chain inclusion is trusted; this does not re-implement Falcon verify.
    """
    if not isinstance(obj, dict):
        return None
    if obj.get("pqsig") == PQSIG_MARKER:
        return None
    sig_type = _sig_type_value(obj)
    if sig_type and sig_type != "pqsig":
        return None
    sig = obj.get("signature")
    if isinstance(sig, dict):
        if "falcon-signature" in sig or "falcon" in sig or "falconsig" in sig:
            if "pqsig" not in sig:
                return None
        pq = sig.get("pqsig")
        if pq is not None:
            if _exclusive_sig_present(sig):
                return None
            parsed = _parse_pqsig_envelope(pq)
            return parsed
        if _exclusive_sig_present(sig):
            return None
        return None
    raw = obj.get("pqsig")
    if raw is not None:
        return _parse_pqsig_envelope(raw)
    return None


def decode_chain_txn(obj) -> dict:
    """Normalize indexer JSON, algod pending JSON, or a SignedTxn-shaped dict."""
    if not isinstance(obj, dict):
        raise AnchorError("invalid chain object")
    if obj.get("pqsig") == PQSIG_MARKER and "signed" in obj:
        raise AnchorError("pqsig marker is not a chain object")
    txn = obj.get("transaction") if isinstance(obj.get("transaction"), dict) else None
    pending = obj.get("txn") if isinstance(obj.get("txn"), dict) else None
    inner = None
    envelope = None
    if txn is not None:
        envelope = txn
        inner = txn.get("payment-transaction") if isinstance(txn.get("payment-transaction"), dict) else {}
        unsigned = txn
    elif pending is not None and isinstance(pending.get("txn"), dict):
        envelope = obj
        unsigned = pending.get("txn")
        inner = unsigned
    elif pending is not None:
        envelope = obj
        unsigned = pending
        inner = pending
    else:
        envelope = obj
        unsigned = obj
        inner = obj.get("payment-transaction") if isinstance(obj.get("payment-transaction"), dict) else obj

    txid = str(
        (txn or {}).get("id")
        or envelope.get("id")
        or obj.get("id")
        or obj.get("txid")
        or obj.get("txId")
        or ""
    ).strip()
    try:
        confirmed_round = int(
            (txn or {}).get("confirmed-round")
            or envelope.get("confirmed-round")
            or obj.get("confirmed-round")
            or obj.get("confirmed_round")
            or 0
        )
    except (TypeError, ValueError):
        confirmed_round = 0
    gen = str(
        unsigned.get("genesis-id")
        or unsigned.get("genesisID")
        or unsigned.get("gen")
        or obj.get("genesis-id")
        or obj.get("genesisID")
        or ""
    ).strip()
    tx_type = str(
        unsigned.get("tx-type")
        or unsigned.get("txType")
        or unsigned.get("type")
        or ""
    ).strip()
    sender = _addr_text(unsigned.get("sender") or unsigned.get("snd"))
    receiver = _addr_text(
        (inner or {}).get("receiver")
        or (inner or {}).get("rcv")
        or unsigned.get("receiver")
        or unsigned.get("rcv")
    )
    try:
        amount = int(
            (inner or {}).get("amount")
            if (inner or {}).get("amount") is not None
            else (unsigned.get("amt") if unsigned.get("amt") is not None else 0)
        )
    except (TypeError, ValueError):
        amount = -1
    try:
        fee = int(unsigned.get("fee") or 0)
    except (TypeError, ValueError):
        fee = -1
    note = unsigned.get("note")
    if isinstance(note, str):
        note = _b64(note)
    elif isinstance(note, (bytes, bytearray)):
        note = bytes(note)
    else:
        note = b""
    close = (
        (inner or {}).get("close-remainder-to")
        or (inner or {}).get("close")
        or unsigned.get("close-remainder-to")
        or unsigned.get("close")
    )
    rekey = unsigned.get("rekey-to") or unsigned.get("rekey")
    group = unsigned.get("group") or unsigned.get("grp")
    lease = unsigned.get("lease") or unsigned.get("lx")
    pq_auth = _pq_auth_from_obj(obj)
    if pq_auth is None and pending is not None:
        pq_auth = _pq_auth_from_obj(pending)
    if pq_auth is None and txn is not None:
        pq_auth = _pq_auth_from_obj(txn)
    if pq_auth is None and isinstance(envelope, dict) and envelope is not obj:
        pq_auth = _pq_auth_from_obj(envelope)
    auth_addr = _auth_addr_field(obj, envelope, txn, pending, unsigned, inner)
    return {
        "txid": txid,
        "confirmed_round": confirmed_round,
        "genesis_id": gen,
        "tx_type": tx_type,
        "sender": sender,
        "receiver": receiver,
        "auth_addr": auth_addr,
        "authorizer": sender,
        "amount": amount,
        "fee": fee,
        "note": note,
        "close": close,
        "rekey": rekey,
        "group": group,
        "lease": lease,
        "has_axfer": "asset-transfer-transaction" in (txn or unsigned)
        or str(tx_type) == "axfer"
        or "aamt" in unsigned
        or "xaid" in unsigned,
        "has_appl": "application-transaction" in (txn or unsigned)
        or str(tx_type) == "appl"
        or "apid" in unsigned,
        "pq_auth": pq_auth,
    }


def _auth_addr_field(*objs):
    """Codec sgnr or REST/indexer auth-addr / authAddr. Empty if self-authorized."""
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        for key in ("sgnr", "auth-addr", "authAddr", "auth_addr"):
            if key not in obj:
                continue
            val = obj.get(key)
            if _nonzero_blob(val):
                return val
    return ""


def verify_fetched_anchor(
    decoded: dict,
    *,
    expected_origin: str,
    expected_size: int,
    expected_root,
    expected_address: str,
    expected_txid: str | None = None,
    expected_network: str | None = None,
) -> dict:
    """Fail closed unless the fetched txn matches PQ1 construction.

    Expected Falcon checkpoint is self-authorized: configured Falcon
    address == sender == receiver == authorizing account. Any nonempty
    AuthAddr (codec sgnr, REST auth-addr / authAddr) fails confirmation.
    Genesis must exactly match expected_network.
    """
    if not isinstance(decoded, dict):
        raise AnchorError("invalid chain object")
    pq_auth = decoded.get("pq_auth")
    if not isinstance(pq_auth, (bytes, bytearray)) or not pq_auth:
        raise AnchorError("falcon authorization missing")
    if bytes(pq_auth) == PQSIG_MARKER.encode("utf-8"):
        raise AnchorError("pqsig marker is not authorization")
    cfg = _network_cfg(expected_network)
    gen = str(decoded.get("genesis_id") or "")
    if gen != cfg.genesis_id:
        raise AnchorError("genesis mismatch")
    addr = (expected_address or "").strip()
    if not addr:
        raise AnchorError("falcon address required")
    if _nonzero_blob(decoded.get("auth_addr")):
        raise AnchorError("auth address forbidden")
    if decoded.get("sender") != addr or decoded.get("receiver") != addr:
        raise AnchorError("sender/receiver mismatch")
    authorizer = decoded.get("authorizer") or decoded.get("sender")
    if authorizer != addr:
        raise AnchorError("authorizer mismatch")
    if int(decoded.get("amount") or 0) != 0:
        raise AnchorError("amount must be 0")
    fee = int(decoded.get("fee") or 0)
    if fee < falcon_min_fee() or fee > MAX_FEE:
        raise AnchorError("fee out of range")
    if _nonzero_blob(decoded.get("close")):
        raise AnchorError("close forbidden")
    if _nonzero_blob(decoded.get("rekey")):
        raise AnchorError("rekey forbidden")
    if _nonzero_blob(decoded.get("group")):
        raise AnchorError("group forbidden")
    if _nonzero_blob(decoded.get("lease")):
        raise AnchorError("lease forbidden")
    if decoded.get("has_axfer") or decoded.get("has_appl"):
        raise AnchorError("axfer/appl forbidden")
    tx_type = str(decoded.get("tx_type") or "")
    if tx_type and tx_type not in {"pay", "payment"}:
        raise AnchorError("not a payment")
    try:
        parsed = decode_note(bytes(decoded.get("note") or b""))
    except Exception as exc:
        raise AnchorError("invalid note") from exc
    if parsed["origin_hash"] != origin_hash(expected_origin):
        raise AnchorError("origin mismatch")
    if int(parsed["tree_size"]) != int(expected_size):
        raise AnchorError("tree size mismatch")
    if isinstance(expected_root, (bytes, bytearray)):
        want_root = bytes(expected_root)
    else:
        try:
            want_root = bytes.fromhex(str(expected_root or ""))
        except ValueError as exc:
            raise AnchorError("invalid root") from exc
    if parsed["root"] != want_root:
        raise AnchorError("root mismatch")
    rnd = int(decoded.get("confirmed_round") or 0)
    if rnd < 1:
        raise AnchorError("not confirmed")
    txid = str(decoded.get("txid") or "").strip()
    if not _looks_like_txid(txid):
        raise AnchorError("invalid confirmed fields")
    if expected_txid and txid != expected_txid.strip():
        raise AnchorError("txid mismatch")
    return {
        "txid": txid,
        "confirmed_round": rnd,
        "tree_size": int(parsed["tree_size"]),
        "origin": expected_origin,
        "root": parsed["root"],
        "pq_auth": bytes(pq_auth),
        "network": cfg.name,
        "genesis_id": cfg.genesis_id,
    }


def is_pq1_construction(decoded: dict, *, expected_address: str = "") -> bool:
    """Structural PQ1 check. Does not confirm inclusion or a checkpoint."""
    if not isinstance(decoded, dict):
        return False
    pq_auth = decoded.get("pq_auth")
    if not isinstance(pq_auth, (bytes, bytearray)) or not pq_auth:
        return False
    if bytes(pq_auth) == PQSIG_MARKER.encode("utf-8"):
        return False
    if _nonzero_blob(decoded.get("auth_addr")):
        return False
    addr = (expected_address or "").strip()
    sender = decoded.get("sender") or ""
    receiver = decoded.get("receiver") or ""
    if addr and (sender != addr or receiver != addr):
        return False
    if sender and receiver and sender != receiver:
        return False
    if int(decoded.get("amount") or 0) != 0:
        return False
    try:
        fee = int(decoded.get("fee") or 0)
    except (TypeError, ValueError):
        return False
    if fee < falcon_min_fee() or fee > MAX_FEE:
        return False
    if _nonzero_blob(decoded.get("close")) or _nonzero_blob(decoded.get("rekey")):
        return False
    if _nonzero_blob(decoded.get("group")) or _nonzero_blob(decoded.get("lease")):
        return False
    if decoded.get("has_axfer") or decoded.get("has_appl"):
        return False
    tx_type = str(decoded.get("tx_type") or "")
    if tx_type and tx_type not in {"pay", "payment"}:
        return False
    try:
        decode_note(bytes(decoded.get("note") or b""))
    except Exception:
        return False
    return True


def classify_falcon_account_txn(decoded: dict, *, expected_address: str = "") -> dict:
    """Unexpected non-PQ1 activity on the Falcon account is an incident."""
    ok = is_pq1_construction(decoded, expected_address=expected_address)
    if ok:
        return {
            "pq1": True,
            "incident": False,
            "alert": "",
        }
    from live402.pq import ops_state

    ops_state.record_non_pq1_incident()
    return {
        "pq1": False,
        "incident": True,
        "alert": "unexpected_non_pq1_txn",
        "severity": "incident",
    }
