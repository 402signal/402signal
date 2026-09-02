"""Router-side 6PN client for 402signal-pq-signer (pq-anchor/1).

TCP JSON-line, one object per connection, newline-terminated. Not HTTP.
Default dial: 402signal-pq-signer.internal:9091. Tests use loopback.

HMAC-SHA256 over versioned canonical bytes (signer internal/hmac @ 076825f):
  pq-anchor/1\\n
  then sorted keys as k=v\\n:
    checkpoint, consistency (nodes joined by comma), origin, request_id,
    root (lowercase hex), timestamp (decimal), tree_size (decimal), v=1
  hmac field is hex(HMAC-SHA256(token, canonical)).

JSON keys sent (exactly these; signer rejects unknown fields):
  v, origin, tree_size, root, consistency, timestamp, request_id, checkpoint, hmac
checkpoint is the Ed25519 signed-note the log already produced, not an
unsigned checkpoint_body. Do not send fee/firstValid/sender/amount/txn.

Reply shape (076825f): {ok:true, tree_size, root, pqsig:"present", signed}
signed is hex of the full SignedTxn. pqsig is the marker "present" (not hex).
Do not treat pqsig as the transaction bytes.

LIVE402_PQ_SIGNER_TOKEN unset/empty: never dial, never sign (C1 live state).
Dial/read timeout is 25s (signer Handle/algokey is 20s plus IPC).
This module never loads a Falcon SK. No Algorand submit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import time
import uuid

from live402.pq import checkpoint as ckpt

TOKEN_ENV = "LIVE402_PQ_SIGNER_TOKEN"
IPC_HOST_ENV = "LIVE402_PQ_SIGNER_HOST"
IPC_PORT_ENV = "LIVE402_PQ_SIGNER_PORT"

# Fly 6PN. Never PORT/8080.
DEFAULT_IPC_HOST = "402signal-pq-signer.internal"
DEFAULT_IPC_PORT = 9091
# Signer Handle/algokey is 20s plus IPC.
IPC_TIMEOUT = 25.0
REQUEST_TTL_SECONDS = 30
_MAX_LINE = 65536
_FORBIDDEN_PORTS = frozenset({8080})

CANONICAL_VERSION = "pq-anchor/1"
REQUEST_VERSION = 1
CANONICAL_KEYS = (
    "checkpoint",
    "consistency",
    "origin",
    "request_id",
    "root",
    "timestamp",
    "tree_size",
    "v",
)
REQUEST_KEYS = (
    "v",
    "origin",
    "tree_size",
    "root",
    "consistency",
    "timestamp",
    "request_id",
    "checkpoint",
    "hmac",
)
# Live signer 076825f reply. signed = hex SignedTxn; pqsig is the marker "present".
REPLY_KEYS = frozenset({"ok", "tree_size", "root", "pqsig", "signed"})
PQSIG_PRESENT = "present"


class SignerClientError(RuntimeError):
    pass


def signer_token() -> str:
    """Shared HMAC token. Empty/unset is the C1 fail-closed state."""
    return (os.environ.get(TOKEN_ENV) or "").strip()


def token_configured() -> bool:
    return bool(signer_token())


def ipc_port(raw: str | int | None = None) -> int:
    """Internal signer port. Never PORT/8080."""
    if raw is None:
        text = (os.environ.get(IPC_PORT_ENV) or "").strip()
        if text:
            try:
                port = int(text)
            except ValueError as exc:
                raise SignerClientError("invalid port") from exc
        else:
            port = DEFAULT_IPC_PORT
    else:
        try:
            port = int(raw)
        except (TypeError, ValueError) as exc:
            raise SignerClientError("invalid port") from exc
    if port in _FORBIDDEN_PORTS:
        raise SignerClientError("signer must not use 8080")
    return port


def ipc_peer_host() -> str:
    explicit = (os.environ.get(IPC_HOST_ENV) or "").strip()
    if explicit:
        return explicit
    return DEFAULT_IPC_HOST


def require_signed_note(text: str) -> str:
    """checkpoint must be a C2SP signed-note, not an unsigned body."""
    if not isinstance(text, str) or not text.strip():
        raise SignerClientError("unsigned checkpoint")
    try:
        ckpt.parse_signed_note(text)
    except ValueError as exc:
        raise SignerClientError("unsigned checkpoint") from exc
    return text


def _hex_node(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    text = str(value).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    try:
        bytes.fromhex(text)
    except ValueError as exc:
        raise SignerClientError("invalid hex") from exc
    return text


def _consistency_csv(nodes) -> str:
    if nodes is None:
        return ""
    if isinstance(nodes, str):
        return ",".join(_hex_node(part) for part in nodes.split(",") if part)
    return ",".join(_hex_node(n) for n in nodes)


def canonical_bytes(
    *,
    origin: str,
    tree_size: int,
    root,
    consistency,
    timestamp: int,
    request_id: str,
    checkpoint: str,
    v: int = REQUEST_VERSION,
) -> bytes:
    """Versioned canonical MAC input. Identical field set/order to signer hmac."""
    if int(v) != REQUEST_VERSION:
        raise SignerClientError("unsupported version")
    fields = {
        "checkpoint": checkpoint if checkpoint is not None else "",
        "consistency": _consistency_csv(consistency),
        "origin": origin if origin is not None else "",
        "request_id": request_id if request_id is not None else "",
        "root": _hex_node(root),
        "timestamp": str(int(timestamp)),
        "tree_size": str(int(tree_size)),
        "v": "1",
    }
    parts = [CANONICAL_VERSION]
    for key in sorted(fields):
        parts.append("%s=%s" % (key, fields[key]))
    return ("\n".join(parts) + "\n").encode("utf-8")


def mac_hex(token: str, canonical: bytes) -> str:
    key = (token or "").encode("utf-8")
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def build_request(
    *,
    origin: str,
    tree_size: int,
    root,
    consistency,
    timestamp: int,
    request_id: str,
    checkpoint: str,
    token: str | None = None,
) -> dict:
    """Exactly REQUEST_KEYS. checkpoint is the signed-note. HMAC covers all fields."""
    secret = token if token is not None else signer_token()
    if not secret:
        raise SignerClientError("token unset")
    note = require_signed_note(checkpoint)
    nodes = [] if consistency is None else list(consistency)
    hex_nodes = [_hex_node(n) for n in nodes]
    root_hex = _hex_node(root)
    body = canonical_bytes(
        origin=origin,
        tree_size=int(tree_size),
        root=root_hex,
        consistency=hex_nodes,
        timestamp=int(timestamp),
        request_id=request_id,
        checkpoint=note,
    )
    payload = {
        "v": REQUEST_VERSION,
        "origin": origin,
        "tree_size": int(tree_size),
        "root": root_hex,
        "consistency": hex_nodes,
        "timestamp": int(timestamp),
        "request_id": request_id,
        "checkpoint": note,
        "hmac": mac_hex(secret, body),
    }
    if set(payload) != set(REQUEST_KEYS):
        raise SignerClientError("request keys")
    if "checkpoint_body" in payload:
        raise SignerClientError("request keys")
    return payload


def encode_request_line(payload: dict) -> str:
    """One JSON object. Refuses unknown keys, unsigned-txn fields, checkpoint_body."""
    if not isinstance(payload, dict) or set(payload) != set(REQUEST_KEYS):
        raise SignerClientError("request keys")
    forbidden = {
        "fee",
        "firstValid",
        "firstRound",
        "sender",
        "snd",
        "amount",
        "amt",
        "txn",
        "unsigned",
        "pk",
        "sk",
        "checkpoint_body",
    }
    if set(payload) & forbidden:
        raise SignerClientError("request keys")
    require_signed_note(str(payload.get("checkpoint") or ""))
    ordered = {key: payload[key] for key in REQUEST_KEYS}
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=True)


def _recv_line(sock: socket.socket, timeout: float) -> str:
    sock.settimeout(timeout)
    buf = bytearray()
    while b"\n" not in buf:
        if len(buf) >= _MAX_LINE:
            raise SignerClientError("line too long")
        chunk = sock.recv(min(4096, _MAX_LINE - len(buf)))
        if not chunk:
            break
        buf.extend(chunk)
    if not buf:
        raise SignerClientError("empty")
    return buf.split(b"\n", 1)[0].decode("utf-8", errors="replace")


def parse_reply(raw: str) -> dict:
    """Live reply {ok, tree_size, root, pqsig:"present", signed}. signed is SignedTxn hex.

    Residual (Ross-only): the response SignedTxn is not yet
    response-MAC authenticated. This parser does not weaken existing
    field checks and does not invent Falcon crypto. Router-side
    persist of AUTHORIZED from these bytes still depends on a future
    signer MAC (protocol version, request_id, origin, tree size, root,
    checkpoint/policy digest, SHA-256 of exact SignedTxn bytes) or
    official native Falcon verify.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SignerClientError("invalid json") from exc
    if not isinstance(data, dict):
        raise SignerClientError("invalid json")
    if data.get("error") or data.get("ok") is not True:
        raise SignerClientError("sign_failed")
    extra = set(data) - REPLY_KEYS
    missing = REPLY_KEYS - set(data)
    if extra or missing:
        raise SignerClientError("sign_failed")
    signed_hex = data.get("signed")
    if not isinstance(signed_hex, str) or not signed_hex:
        raise SignerClientError("sign_failed")
    try:
        signed = bytes.fromhex(signed_hex)
    except ValueError as exc:
        raise SignerClientError("sign_failed") from exc
    if not signed:
        raise SignerClientError("sign_failed")
    pqsig = data.get("pqsig")
    # Live marker is "present" (case-sensitive). Not hex. Never the txn.
    if pqsig != PQSIG_PRESENT:
        raise SignerClientError("sign_failed")
    if signed == pqsig.encode("utf-8"):
        raise SignerClientError("sign_failed")
    return {
        "ok": True,
        "tree_size": int(data["tree_size"]),
        "root": _hex_node(data["root"]),
        "pqsig": PQSIG_PRESENT,
        "signed": signed,
    }


def _int64_be(value) -> bytes:
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise SignerClientError("sign_failed") from exc
    if n < 0:
        raise SignerClientError("sign_failed")
    try:
        return n.to_bytes(8, "big", signed=False)
    except OverflowError as exc:
        raise SignerClientError("sign_failed") from exc


def roots_equal(left, right) -> bool:
    """Canonical root compare. Accepts bytes or hex. Constant-time on equal length."""
    try:
        a = bytes.fromhex(_hex_node(left))
        b = bytes.fromhex(_hex_node(right))
    except (SignerClientError, ValueError, TypeError):
        return False
    if len(a) != len(b):
        return False
    return hmac.compare_digest(a, b)


def bind_reply(reply: dict, *, tree_size: int, root) -> dict:
    """Fail closed unless reply.tree_size and reply.root match the request."""
    if not isinstance(reply, dict):
        raise SignerClientError("sign_failed")
    try:
        have = _int64_be(reply.get("tree_size"))
        want = _int64_be(tree_size)
    except SignerClientError:
        raise
    except Exception as exc:
        raise SignerClientError("sign_failed") from exc
    if not hmac.compare_digest(have, want):
        raise SignerClientError("sign_failed")
    if not roots_equal(reply.get("root"), root):
        raise SignerClientError("sign_failed")
    signed = reply.get("signed")
    if not isinstance(signed, (bytes, bytearray)) or not signed:
        raise SignerClientError("sign_failed")
    return reply


def request_signed(
    *,
    origin: str,
    tree_size: int,
    root,
    consistency,
    checkpoint: str,
    now: int | None = None,
    request_id: str | None = None,
    host: str | None = None,
    port: int | None = None,
    timeout: float = IPC_TIMEOUT,
    token: str | None = None,
) -> dict:
    """Dial once. Returns parsed reply. Never dials if token unset. Does not POST."""
    secret = token if token is not None else signer_token()
    if not secret:
        raise SignerClientError("token unset")
    dest_host = (host or ipc_peer_host()).strip()
    dest_port = ipc_port() if port is None else ipc_port(port)
    ts = int(now if now is not None else time.time())
    rid = request_id or uuid.uuid4().hex
    payload = build_request(
        origin=origin,
        tree_size=tree_size,
        root=root,
        consistency=consistency,
        timestamp=ts,
        request_id=rid,
        checkpoint=checkpoint,
        token=secret,
    )
    line = encode_request_line(payload) + "\n"
    try:
        with socket.create_connection((dest_host, dest_port), timeout=timeout) as sock:
            sock.sendall(line.encode("utf-8"))
            raw = _recv_line(sock, timeout)
    except SignerClientError:
        raise
    except Exception as exc:
        raise SignerClientError("unavailable") from exc
    data = parse_reply(raw)
    bind_reply(data, tree_size=tree_size, root=root)
    return data


def request_sign(
    *,
    origin: str,
    tree_size: int,
    root,
    consistency,
    checkpoint: str,
    now: int | None = None,
    request_id: str | None = None,
    host: str | None = None,
    port: int | None = None,
    timeout: float = IPC_TIMEOUT,
    token: str | None = None,
) -> bytes:
    """Return SignedTxn bytes only after reply tree_size and root match the request."""
    data = request_signed(
        origin=origin,
        tree_size=tree_size,
        root=root,
        consistency=consistency,
        checkpoint=checkpoint,
        now=now,
        request_id=request_id,
        host=host,
        port=port,
        timeout=timeout,
        token=token,
    )
    return data["signed"]
