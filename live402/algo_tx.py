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


def _mp_read_uint(buf: bytes, i: int) -> tuple[int, int]:
    if i >= len(buf):
        raise ValueError("truncated msgpack")
    b = buf[i]
    if b < 128:
        return b, i + 1
    if b == 0xCC:
        return buf[i + 1], i + 2
    if b == 0xCD:
        return int.from_bytes(buf[i + 1 : i + 3], "big"), i + 3
    if b == 0xCE:
        return int.from_bytes(buf[i + 1 : i + 5], "big"), i + 5
    if b == 0xCF:
        return int.from_bytes(buf[i + 1 : i + 9], "big"), i + 9
    raise ValueError("unsupported msgpack int")


def _mp_read(buf: bytes, i: int):
    if i >= len(buf):
        raise ValueError("truncated msgpack")
    b = buf[i]
    if b == 0xC2:
        return False, i + 1
    if b == 0xC3:
        return True, i + 1
    if b < 128 or b in (0xCC, 0xCD, 0xCE, 0xCF):
        return _mp_read_uint(buf, i)
    if 0xA0 <= b <= 0xBF:
        n = b - 0xA0
        return buf[i + 1 : i + 1 + n].decode("utf-8"), i + 1 + n
    if b == 0xD9:
        n = buf[i + 1]
        return buf[i + 2 : i + 2 + n].decode("utf-8"), i + 2 + n
    if b == 0xC4:
        n = buf[i + 1]
        return bytes(buf[i + 2 : i + 2 + n]), i + 2 + n
    if b == 0xC5:
        n = int.from_bytes(buf[i + 1 : i + 3], "big")
        return bytes(buf[i + 3 : i + 3 + n]), i + 3 + n
    if b == 0xC6:
        n = int.from_bytes(buf[i + 1 : i + 5], "big")
        return bytes(buf[i + 5 : i + 5 + n]), i + 5 + n
    if 0x80 <= b <= 0x8F:
        n = b - 0x80
        i += 1
        out = {}
        for _ in range(n):
            key, i = _mp_read(buf, i)
            val, i = _mp_read(buf, i)
            out[str(key)] = val
        return out, i
    if b == 0xDE:
        n = int.from_bytes(buf[i + 1 : i + 3], "big")
        i += 3
        out = {}
        for _ in range(n):
            key, i = _mp_read(buf, i)
            val, i = _mp_read(buf, i)
            out[str(key)] = val
        return out, i
    if 0x90 <= b <= 0x9F:
        n = b - 0x90
        i += 1
        items = []
        for _ in range(n):
            val, i = _mp_read(buf, i)
            items.append(val)
        return items, i
    raise ValueError("unsupported msgpack type")


def msgpack_decode(raw: bytes) -> dict:
    """Minimal decoder for Algorand SignedTxn maps we encode. Fail closed."""
    obj, end = _mp_read(bytes(raw), 0)
    if end != len(raw) or not isinstance(obj, dict):
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
    """txid of the unsigned txn inside a SignedTxn msgpack blob."""
    obj = msgpack_decode(bytes(signed))
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
