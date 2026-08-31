"""C2SP tlog-checkpoint@v1.0.0 + signed-note@v1.0.0.

Log signatures are Ed25519 type 0x01. Key name = origin.
Witness type 0x04 is parsed; default policy is empty (no fake independent witness).
Do not Falcon the log signature.
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import TYPE_CHECKING

from live402.pq import ORIGIN
from live402.pq.merkle import HASH_SIZE

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SIG_ED25519 = 0x01
SIG_WITNESS = 0x04
EMDASH = "\u2014"
NOTE_LINE = re.compile(r"^" + EMDASH + r" (\S+) (\S+)$")


def key_id(name: str, sig_type: int, public_key: bytes) -> bytes:
    return hashlib.sha256(name.encode("utf-8") + b"\n" + bytes([sig_type]) + public_key).digest()[:4]


def vkey_encode(name: str, public_key: bytes, sig_type: int = SIG_ED25519) -> str:
    kid = key_id(name, sig_type, public_key)
    payload = bytes([sig_type]) + public_key
    return "%s+%s+%s" % (name, kid.hex(), base64.b64encode(payload).decode("ascii"))


def vkey_parse(text: str) -> dict:
    raw = (text or "").strip()
    first = raw.find("+")
    second = raw.find("+", first + 1) if first >= 0 else -1
    if first < 1 or second < 0 or second + 1 >= len(raw):
        raise ValueError("invalid vkey")
    name, kid_hex, blob_b64 = raw[:first], raw[first + 1 : second], raw[second + 1 :]
    try:
        kid = bytes.fromhex(kid_hex)
        blob = base64.b64decode(blob_b64)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid vkey") from exc
    if len(kid) != 4 or len(blob) < 2:
        raise ValueError("invalid vkey")
    sig_type = blob[0]
    pk = blob[1:]
    if sig_type == SIG_ED25519 and len(pk) != 32:
        raise ValueError("invalid ed25519 vkey")
    if key_id(name, sig_type, pk) != kid:
        raise ValueError("vkey id mismatch")
    return {"name": name, "key_id": kid, "sig_type": sig_type, "public_key": pk}


def checkpoint_body(origin: str, tree_size: int, root: bytes) -> str:
    if not origin or "\n" in origin:
        raise ValueError("invalid origin")
    if tree_size < 0:
        raise ValueError("negative tree size")
    if len(root) != HASH_SIZE:
        raise ValueError("root must be 32 bytes")
    size_s = str(int(tree_size))
    if size_s != "0" and size_s.startswith("0"):
        raise ValueError("tree size leading zeroes")
    return "%s\n%s\n%s\n" % (origin, size_s, base64.b64encode(root).decode("ascii"))


def parse_checkpoint_body(text: str) -> dict:
    raw = text if text.endswith("\n") else text + "\n"
    lines = raw.split("\n")
    # body is origin, size, hash, then optional extensions; last element is empty from trailing newline
    if len(lines) < 4:
        raise ValueError("checkpoint body too short")
    origin = lines[0]
    size_s = lines[1]
    hash_s = lines[2]
    if not origin:
        raise ValueError("empty origin")
    if not size_s.isdigit() or (size_s.startswith("0") and size_s != "0"):
        raise ValueError("invalid tree size")
    try:
        root = base64.b64decode(hash_s, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid root hash") from exc
    if len(root) != HASH_SIZE:
        raise ValueError("root must be 32 bytes")
    extensions = [ln for ln in lines[3:-1] if ln]
    return {
        "origin": origin,
        "tree_size": int(size_s),
        "root": root,
        "extensions": extensions,
        "text": "%s\n%s\n%s\n" % (origin, size_s, hash_s)
        + ("".join(e + "\n" for e in extensions)),
    }


def sign_note(body: str, name: str, private_key: "Ed25519PrivateKey") -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    if not body.endswith("\n"):
        body = body + "\n"
    pk = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    kid = key_id(name, SIG_ED25519, pk)
    sig = private_key.sign(body.encode("utf-8"))
    blob = base64.b64encode(kid + sig).decode("ascii")
    return "%s\n%s %s %s\n" % (body, EMDASH, name, blob)


def parse_signed_note(note: str) -> dict:
    if not isinstance(note, str) or "\x00" in note:
        raise ValueError("invalid note")
    if EMDASH + " " not in note:
        raise ValueError("unsigned note")
    # last blank line separates text from signatures
    parts = note.split("\n")
    if not parts or parts[-1] != "":
        if note.endswith("\n"):
            parts = note.split("\n")
        else:
            parts = (note + "\n").split("\n")
    # find last empty line
    blank = None
    for i, line in enumerate(parts[:-1]):
        if line == "":
            blank = i
    if blank is None:
        raise ValueError("missing signature separator")
    text_lines = parts[:blank]
    if not text_lines:
        raise ValueError("empty note text")
    text = "\n".join(text_lines) + "\n"
    sigs = []
    for line in parts[blank + 1 :]:
        if line == "":
            continue
        m = NOTE_LINE.match(line)
        if not m:
            raise ValueError("invalid signature line")
        name, b64 = m.group(1), m.group(2)
        try:
            raw = base64.b64decode(b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid signature encoding") from exc
        if len(raw) < 4 + 64:
            raise ValueError("signature too short")
        kid, rest = raw[:4], raw[4:]
        sigs.append({"name": name, "key_id": kid, "payload": rest, "raw": raw})
    if not sigs:
        raise ValueError("no signatures")
    return {"text": text, "signatures": sigs, "note": note if note.endswith("\n") else note + "\n"}


def verify_signed_note(note: str, vkey: str) -> dict:
    parsed = parse_signed_note(note)
    key = vkey_parse(vkey)
    if key["sig_type"] != SIG_ED25519:
        raise ValueError("log vkey must be ed25519 type 0x01")
    matched = False
    for sig in parsed["signatures"]:
        if sig["name"] != key["name"] or sig["key_id"] != key["key_id"]:
            continue
        if len(sig["payload"]) != 64:
            raise ValueError("invalid ed25519 signature length")
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        pub = Ed25519PublicKey.from_public_bytes(key["public_key"])
        try:
            pub.verify(sig["payload"], parsed["text"].encode("utf-8"))
        except InvalidSignature as exc:
            raise ValueError("checkpoint signature failed") from exc
        matched = True
        break
    if not matched:
        raise ValueError("no signature from trusted log key")
    body = parse_checkpoint_body(parsed["text"])
    return {"note": parsed["note"], "body": body, "signatures": parsed["signatures"]}


def parse_witness_sig(sig: dict) -> dict | None:
    """Support tlog-cosignature 0x04. Does not invent a 402Signal witness."""
    payload = sig.get("payload") or b""
    # timestamp 8 + ed25519 64 = 72
    if len(payload) != 72:
        return None
    ts = int.from_bytes(payload[:8], "big")
    return {"type": SIG_WITNESS, "timestamp": ts, "signature": payload[8:], "name": sig.get("name")}


def sign_checkpoint(origin: str, tree_size: int, root: bytes, private_key: "Ed25519PrivateKey") -> str:
    body = checkpoint_body(origin or ORIGIN, tree_size, root)
    return sign_note(body, origin or ORIGIN, private_key)
