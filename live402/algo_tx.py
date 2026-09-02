"""Algorand unsigned group helper. Stdlib only. No keys, no seeds."""

from __future__ import annotations

import base64
import hashlib

from live402 import algod

GENESIS_ID = algod.GENESIS_ID
GENESIS_HASH_B64 = algod.GENESIS_HASH
NOTE_FEE_PAYER = b"x402-fee-payer"
NOTE_PAYMENT = b"x402-payment-v2"
HELPER_EXTRA_KEYS = ("suggestedParams", "unsignedGroup", "decimals")


def sha512_256(data: bytes) -> bytes:
    return hashlib.new("sha512_256", data).digest()


def decode_address(addr: str) -> bytes:
    raw_addr = (addr or "").strip()
    if len(raw_addr) != 58:
        raise ValueError("invalid algorand address")
    padded = raw_addr + ("=" * ((8 - len(raw_addr) % 8) % 8))
    decoded = base64.b32decode(padded)
    if len(decoded) != 36:
        raise ValueError("invalid algorand address")
    key, checksum = decoded[:32], decoded[32:]
    if sha512_256(key)[-4:] != checksum:
        raise ValueError("invalid algorand address checksum")
    return key


def encode_address(key: bytes) -> str:
    raw = bytes(key)
    if len(raw) != 32:
        raise ValueError("invalid algorand public key")
    checksum = sha512_256(raw)[-4:]
    return base64.b32encode(raw + checksum).decode("ascii").rstrip("=")


def _mp_uint(n: int) -> bytes:
    if n < 0:
        raise ValueError("negative integer")
    if n < 128:
        return bytes([n])
    if n < 256:
        return b"\xcc" + bytes([n])
    if n < 65536:
        return b"\xcd" + n.to_bytes(2, "big")
    if n < 2**32:
        return b"\xce" + n.to_bytes(4, "big")
    return b"\xcf" + n.to_bytes(8, "big")


def _mp_str(s: str) -> bytes:
    raw = s.encode("utf-8")
    n = len(raw)
    if n < 32:
        return bytes([0xA0 + n]) + raw
    if n < 256:
        return b"\xd9" + bytes([n]) + raw
    raise ValueError("string too long")


def _mp_bin(b: bytes) -> bytes:
    n = len(b)
    if n < 256:
        return b"\xc4" + bytes([n]) + b
    if n < 65536:
        return b"\xc5" + n.to_bytes(2, "big") + b
    return b"\xc6" + n.to_bytes(4, "big") + b


def _mp_array(items: list[bytes]) -> bytes:
    n = len(items)
    if n < 16:
        head = bytes([0x90 + n])
    elif n < 65536:
        head = b"\xdc" + n.to_bytes(2, "big")
    else:
        raise ValueError("array too long")
    return head + b"".join(items)


def _mp_map(pairs: list[tuple[str, bytes]]) -> bytes:
    n = len(pairs)
    if n < 16:
        head = bytes([0x80 + n])
    elif n < 65536:
        head = b"\xde" + n.to_bytes(2, "big")
    else:
        raise ValueError("map too long")
    out = [head]
    for key, val in pairs:
        out.append(_mp_str(key))
        out.append(val)
    return b"".join(out)


def msgpack_encode(d: dict) -> bytes:
    pairs: list[tuple[str, bytes]] = []
    for key in sorted(d.keys()):
        val = d[key]
        if val is None:
            continue
        if isinstance(val, bool):
            encoded = b"\xc3" if val else b"\xc2"
        elif isinstance(val, int):
            encoded = _mp_uint(val)
        elif isinstance(val, str):
            encoded = _mp_str(val)
        elif isinstance(val, (bytes, bytearray)):
            encoded = _mp_bin(bytes(val))
        elif isinstance(val, dict):
            encoded = msgpack_encode(val)
        elif isinstance(val, list):
            encoded = _mp_array(
                [_mp_bin(x) if isinstance(x, (bytes, bytearray)) else _mp_uint(int(x)) for x in val]
            )
        else:
            raise TypeError("unsupported msgpack type")
        pairs.append((str(key), encoded))
    return _mp_map(pairs)


def encode_unsigned(txn: dict) -> bytes:
    return msgpack_encode(txn)


def _mp_need(buf: bytes, i: int, n: int) -> None:
    if i + n > len(buf):
        raise ValueError("truncated msgpack")


def _mp_read_uint(buf: bytes, i: int, *, strict: bool = False) -> tuple[int, int]:
    if i >= len(buf):
        raise ValueError("truncated msgpack")
    b = buf[i]
    if b < 128:
        return b, i + 1
    if b == 0xCC:
        _mp_need(buf, i, 2)
        n = buf[i + 1]
        if strict and n < 128:
            raise ValueError("non-minimal msgpack int")
        return n, i + 2
    if b == 0xCD:
        _mp_need(buf, i, 3)
        n = int.from_bytes(buf[i + 1 : i + 3], "big")
        if strict and n < 256:
            raise ValueError("non-minimal msgpack int")
        return n, i + 3
    if b == 0xCE:
        _mp_need(buf, i, 5)
        n = int.from_bytes(buf[i + 1 : i + 5], "big")
        if strict and n < 65536:
            raise ValueError("non-minimal msgpack int")
        return n, i + 5
    if b == 0xCF:
        _mp_need(buf, i, 9)
        n = int.from_bytes(buf[i + 1 : i + 9], "big")
        if strict and n < 2**32:
            raise ValueError("non-minimal msgpack int")
        return n, i + 9
    raise ValueError("unsupported msgpack int")


def _mp_read_sint(buf: bytes, i: int) -> tuple[int, int]:
    """Signed msgpack ints, including negative fixints (0xE0-0xFF)."""
    if i >= len(buf):
        raise ValueError("truncated msgpack")
    b = buf[i]
    if b >= 0xE0:
        return b - 256, i + 1
    if b == 0xD0:
        _mp_need(buf, i, 2)
        return int.from_bytes(buf[i + 1 : i + 2], "big", signed=True), i + 2
    if b == 0xD1:
        _mp_need(buf, i, 3)
        return int.from_bytes(buf[i + 1 : i + 3], "big", signed=True), i + 3
    if b == 0xD2:
        _mp_need(buf, i, 5)
        return int.from_bytes(buf[i + 1 : i + 5], "big", signed=True), i + 5
    if b == 0xD3:
        _mp_need(buf, i, 9)
        return int.from_bytes(buf[i + 1 : i + 9], "big", signed=True), i + 9
    raise ValueError("unsupported msgpack int")


def _mp_str_payload(raw: bytes):
    """UTF-8 text when valid; raw bytes otherwise (go-algorand Raw []byte)."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return bytes(raw)


def _mp_map_key(key, *, strict: bool = False) -> str:
    if isinstance(key, str):
        if not key:
            raise ValueError("empty msgpack map key")
        return key
    if strict:
        raise ValueError("non-text msgpack map key")
    if isinstance(key, (bytes, bytearray)):
        try:
            return bytes(key).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("invalid msgpack map key") from exc
    return str(key)


_MAX_MSGPACK_DEPTH = 8
# Hard cap on inbound decode. Falcon-1024 SignedTxn estimate is ~3.5KiB
# (max pk 1793 + max sig 1423 + pay fields). 8192 rejects hostile blobs.
MAX_MSGPACK_BYTES = 8192


def _mp_read_map(buf: bytes, i: int, n: int, *, depth: int = 0, strict: bool = False):
    if depth > _MAX_MSGPACK_DEPTH:
        raise ValueError("excessive msgpack nesting")
    out = {}
    prev = None
    for _ in range(n):
        key, i = _mp_read(buf, i, depth=depth + 1, strict=strict)
        val, i = _mp_read(buf, i, depth=depth + 1, strict=strict)
        mapped = _mp_map_key(key, strict=strict)
        if mapped in out:
            raise ValueError("duplicate msgpack key")
        if strict and prev is not None and mapped < prev:
            raise ValueError("unordered msgpack key")
        out[mapped] = val
        prev = mapped
    return out, i


def _mp_read_array(buf: bytes, i: int, n: int, *, depth: int = 0, strict: bool = False):
    if depth > _MAX_MSGPACK_DEPTH:
        raise ValueError("excessive msgpack nesting")
    items = []
    for _ in range(n):
        val, i = _mp_read(buf, i, depth=depth + 1, strict=strict)
        items.append(val)
    return items, i


def _mp_read(buf: bytes, i: int, *, depth: int = 0, strict: bool = False):
    """Decode one msgpack value. Algorand/go-msgpack types used in SignedTxn."""
    if depth > _MAX_MSGPACK_DEPTH:
        raise ValueError("excessive msgpack nesting")
    if i >= len(buf):
        raise ValueError("truncated msgpack")
    b = buf[i]
    if b == 0xC0:
        if strict:
            raise ValueError("msgpack nil forbidden")
        return None, i + 1
    if b == 0xC2:
        if strict:
            raise ValueError("msgpack bool forbidden")
        return False, i + 1
    if b == 0xC3:
        if strict:
            raise ValueError("msgpack bool forbidden")
        return True, i + 1
    if b < 128 or b in (0xCC, 0xCD, 0xCE, 0xCF):
        return _mp_read_uint(buf, i, strict=strict)
    if b >= 0xE0 or b in (0xD0, 0xD1, 0xD2, 0xD3):
        if strict:
            raise ValueError("negative txn integer")
        return _mp_read_sint(buf, i)
    if 0xA0 <= b <= 0xBF:
        n = b - 0xA0
        _mp_need(buf, i + 1, n)
        return _mp_str_payload(buf[i + 1 : i + 1 + n]), i + 1 + n
    if b == 0xD9:
        _mp_need(buf, i, 2)
        n = buf[i + 1]
        if strict and n < 32:
            raise ValueError("non-minimal msgpack str")
        _mp_need(buf, i + 2, n)
        return _mp_str_payload(buf[i + 2 : i + 2 + n]), i + 2 + n
    if b == 0xDA:
        _mp_need(buf, i, 3)
        n = int.from_bytes(buf[i + 1 : i + 3], "big")
        if strict and n < 256:
            raise ValueError("non-minimal msgpack str")
        _mp_need(buf, i + 3, n)
        # str16 is go-algorand Raw []byte for Falcon pk/sig (n >= 256).
        return bytes(buf[i + 3 : i + 3 + n]), i + 3 + n
    if b == 0xDB:
        if strict:
            raise ValueError("unproven msgpack form")
        _mp_need(buf, i, 5)
        n = int.from_bytes(buf[i + 1 : i + 5], "big")
        _mp_need(buf, i + 5, n)
        return bytes(buf[i + 5 : i + 5 + n]), i + 5 + n
    if b == 0xC4:
        _mp_need(buf, i, 2)
        n = buf[i + 1]
        _mp_need(buf, i + 2, n)
        return bytes(buf[i + 2 : i + 2 + n]), i + 2 + n
    if b == 0xC5:
        _mp_need(buf, i, 3)
        n = int.from_bytes(buf[i + 1 : i + 3], "big")
        if strict and n < 256:
            raise ValueError("non-minimal msgpack bin")
        _mp_need(buf, i + 3, n)
        return bytes(buf[i + 3 : i + 3 + n]), i + 3 + n
    if b == 0xC6:
        if strict:
            raise ValueError("unproven msgpack form")
        _mp_need(buf, i, 5)
        n = int.from_bytes(buf[i + 1 : i + 5], "big")
        _mp_need(buf, i + 5, n)
        return bytes(buf[i + 5 : i + 5 + n]), i + 5 + n
    if 0x80 <= b <= 0x8F:
        return _mp_read_map(buf, i + 1, b - 0x80, depth=depth, strict=strict)
    if b == 0xDE:
        _mp_need(buf, i, 3)
        n = int.from_bytes(buf[i + 1 : i + 3], "big")
        if strict and n < 16:
            raise ValueError("non-minimal msgpack map")
        return _mp_read_map(buf, i + 3, n, depth=depth, strict=strict)
    if b == 0xDF:
        if strict:
            raise ValueError("unproven msgpack form")
        _mp_need(buf, i, 5)
        n = int.from_bytes(buf[i + 1 : i + 5], "big")
        return _mp_read_map(buf, i + 5, n, depth=depth, strict=strict)
    if 0x90 <= b <= 0x9F:
        return _mp_read_array(buf, i + 1, b - 0x90, depth=depth, strict=strict)
    if b == 0xDC:
        _mp_need(buf, i, 3)
        n = int.from_bytes(buf[i + 1 : i + 3], "big")
        if strict and n < 16:
            raise ValueError("non-minimal msgpack array")
        return _mp_read_array(buf, i + 3, n, depth=depth, strict=strict)
    if b == 0xDD:
        if strict:
            raise ValueError("unproven msgpack form")
        _mp_need(buf, i, 5)
        n = int.from_bytes(buf[i + 1 : i + 5], "big")
        return _mp_read_array(buf, i + 5, n, depth=depth, strict=strict)
    raise ValueError("unsupported msgpack type")


def msgpack_decode(raw: bytes, *, strict: bool = False) -> dict:
    """Decoder for Algorand SignedTxn maps (msgp + go-codec Raw). Fail closed.

    Duplicate keys and a raw-byte size cap are always enforced.
    strict=True is the SignedTxn path: negative integers, nil/bools,
    non-text / empty / descending keys, non-minimal encodings, trailing
    data, excessive nesting, and unproven alternate forms
    (str32/bin32/map32/array32) are rejected. Descending keys are a
    decoder hygiene rule, not a claim of official Algorand canonical
    encoding. The confirmed tree-4 algokey fixture happens to be
    lexicographic (pqsig/txn, pk/sch/sig). Do not require re-encoding
    equality.
    """
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise TypeError("msgpack bytes required")
    blob = bytes(raw)
    if len(blob) > MAX_MSGPACK_BYTES:
        raise ValueError("msgpack too large")
    obj, end = _mp_read(blob, 0, depth=0, strict=strict)
    if end != len(blob) or not isinstance(obj, dict):
        raise ValueError("invalid msgpack map")
    return obj


def txid_from_unsigned(txn: dict) -> str:
    """Official Algorand txid: base32(SHA512_256('TX' || msgpack(unsigned))).

    52-character no-pad base32. Same algorithm as the SDK Transaction.get_txid.
    """
    if not isinstance(txn, dict):
        raise ValueError("unsigned txn required")
    raw = encode_unsigned(txn)
    digest = sha512_256(b"TX" + raw)
    return base64.b32encode(digest).decode("ascii").rstrip("=")


def txid_from_signed(signed: bytes) -> str:
    """txid of the unsigned txn inside a SignedTxn msgpack blob.

    Uses the exact inbound bytes. Does not rewrite pqsig. strict decode
    rejects unproven encodings; unsigned txn fields are then hashed.
    """
    obj = msgpack_decode(bytes(signed), strict=True)
    txn = obj.get("txn") if isinstance(obj, dict) else None
    if not isinstance(txn, dict):
        raise ValueError("signed txn missing txn")
    return txid_from_unsigned(txn)


def calculate_group_id(txns: list[dict]) -> bytes:
    txids = []
    for txn in txns:
        raw = encode_unsigned(txn)
        txids.append(sha512_256(b"TX" + raw))
    grouped = msgpack_encode({"txlist": txids})
    return sha512_256(b"TG" + grouped)


def pay_txn(sender, receiver, amount, fee, first, last, genesis_id, genesis_hash, note=None, group=None):
    d: dict = {}
    if amount:
        d["amt"] = int(amount)
    if fee:
        d["fee"] = int(fee)
    if first:
        d["fv"] = int(first)
    if genesis_id:
        d["gen"] = genesis_id
    if genesis_hash:
        d["gh"] = genesis_hash
    if group:
        d["grp"] = group
    d["lv"] = int(last)
    if note:
        d["note"] = note
    d["rcv"] = decode_address(receiver)
    d["snd"] = decode_address(sender)
    d["type"] = "pay"
    return d


def axfer_txn(sender, receiver, amount, asset, fee, first, last, genesis_id, genesis_hash, note=None, group=None):
    d: dict = {}
    if amount:
        d["aamt"] = int(amount)
    d["arcv"] = decode_address(receiver)
    if fee:
        d["fee"] = int(fee)
    if first:
        d["fv"] = int(first)
    if genesis_id:
        d["gen"] = genesis_id
    if genesis_hash:
        d["gh"] = genesis_hash
    if group:
        d["grp"] = group
    d["lv"] = int(last)
    if note:
        d["note"] = note
    d["snd"] = decode_address(sender)
    d["type"] = "axfer"
    if asset:
        d["xaid"] = int(asset)
    return d


def unsigned_group_template(fee_payer: str, pay_to: str, asset: str, amount: str, min_fee: int) -> dict:
    pooled = max(int(min_fee or 1000), 1000) * 2
    return {
        "paymentIndex": 1,
        "feePayerTxn": {
            "type": "pay",
            "from": fee_payer,
            "to": fee_payer,
            "amount": 0,
            "fee": pooled,
            "note": NOTE_FEE_PAYER.decode("ascii"),
            "flatFee": True,
        },
        "paymentTxn": {
            "type": "axfer",
            "to": pay_to,
            "amount": int(amount),
            "asset": int(asset),
            "fee": 0,
            "note": NOTE_PAYMENT.decode("ascii"),
            "flatFee": True,
        },
    }


def build_unsigned_group(sender, fee_payer, pay_to, amount, asset, params):
    first = int(params.get("firstValid") or params.get("firstRound"))
    last = int(params.get("lastValid") or params.get("lastRound"))
    gen = str(params.get("genesisID") or GENESIS_ID)
    gh = base64.b64decode(str(params.get("genesisHash") or GENESIS_HASH_B64))
    min_fee = int(params.get("minFee") or 1000)
    fee_payer_fee = max(min_fee, 1000) * 2
    fee = pay_txn(fee_payer, fee_payer, 0, fee_payer_fee, first, last, gen, gh, note=NOTE_FEE_PAYER)
    pay = axfer_txn(sender, pay_to, int(amount), int(asset), 0, first, last, gen, gh, note=NOTE_PAYMENT)
    gid = calculate_group_id([fee, pay])
    fee["grp"] = gid
    pay["grp"] = gid
    group = [
        base64.b64encode(encode_unsigned(fee)).decode("ascii"),
        base64.b64encode(encode_unsigned(pay)).decode("ascii"),
    ]
    return group, 1


def algorand_accept_extra(fee_payer, pay_to, asset, amount, sender=None):
    params = algod.suggested_params()
    min_fee = int((params or {}).get("minFee") or 1000)
    extra = {
        "decimals": 6,
        "unsignedGroup": unsigned_group_template(fee_payer, pay_to, asset, amount, min_fee),
    }
    if params:
        extra["suggestedParams"] = params
    sender_s = (sender or "").strip()
    if sender_s and params and (params.get("firstValid") or params.get("firstRound")):
        try:
            txns, index = build_unsigned_group(sender_s, fee_payer, pay_to, int(amount), int(asset), params)
            extra["unsignedGroup"] = dict(extra["unsignedGroup"])
            extra["unsignedGroup"]["txns"] = txns
            extra["unsignedGroup"]["paymentIndex"] = index
            extra["unsignedGroup"]["sender"] = sender_s
        except (ValueError, TypeError, KeyError):
            pass
    return extra
