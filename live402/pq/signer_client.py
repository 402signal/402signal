"""Router-side 6PN client for 402signal-pq-signer (pq-anchor/1).

TCP JSON-line, one object per connection, newline-terminated. Not HTTP.
Default dial: 402signal-pq-signer.internal:9091. Tests use loopback.

HMAC-SHA256 over versioned canonical bytes (signer internal/hmac @ 076825f):
  pq-anchor/1\\n
  then sorted keys as k=v\\n:
    checkpoint, consistency (nodes joined by comma), origin, request_id,
    root (lowercase hex), timestamp (decimal), tree_size (decimal), v=1
  hmac field is hex(HMAC-SHA256(token, canonical)).
Signer compares MAC; this client only generates the same canonical bytes.

JSON keys sent (exactly these; signer rejects unknown fields):
  v, origin, tree_size, root, consistency, timestamp, request_id, checkpoint, hmac
Do not send fee/firstValid/sender/amount/unsigned txn. Signer builds PaymentTxn.

LIVE402_PQ_SIGNER_TOKEN unset/empty: never dial, never sign (C1 live state).
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

TOKEN_ENV = "LIVE402_PQ_SIGNER_TOKEN"
IPC_HOST_ENV = "LIVE402_PQ_SIGNER_HOST"
IPC_PORT_ENV = "LIVE402_PQ_SIGNER_PORT"

# Fly 6PN. Never PORT/8080.
DEFAULT_IPC_HOST = "402signal-pq-signer.internal"
DEFAULT_IPC_PORT = 9091
IPC_TIMEOUT = 2.0
# Signer rejects stale timestamp; router sends now (short window).
REQUEST_TTL_SECONDS = 30
_MAX_LINE = 65536
_FORBIDDEN_PORTS = frozenset({8080})

CANONICAL_VERSION = "pq-anchor/1"
REQUEST_VERSION = 1
# Sorted HMAC keys. Must match signer internal/hmac @ 076825f.
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
# Wire object keys, exactly, in this order. No extras.
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
    # sorted keys as k=v\n after the version line
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
    """Exactly REQUEST_KEYS. HMAC covers the canonical set, not a JSON subset."""
    secret = token if token is not None else signer_token()
    if not secret:
        raise SignerClientError("token unset")
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
        checkpoint=checkpoint,
    )
    payload = {
        "v": REQUEST_VERSION,
        "origin": origin,
        "tree_size": int(tree_size),
        "root": root_hex,
        "consistency": hex_nodes,
        "timestamp": int(timestamp),
        "request_id": request_id,
        "checkpoint": checkpoint,
        "hmac": mac_hex(secret, body),
    }
    if set(payload) != set(REQUEST_KEYS):
        raise SignerClientError("request keys")
    return payload


def encode_request_line(payload: dict) -> str:
    """One JSON object. Refuses unknown keys and unsigned-txn fields."""
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
    }
    if set(payload) & forbidden:
        raise SignerClientError("request keys")
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


def _parse_reply(raw: str) -> bytes:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SignerClientError("invalid json") from exc
    if not isinstance(data, dict) or data.get("error"):
        raise SignerClientError("sign_failed")
    hex_sig = data.get("pqsig")
    if not isinstance(hex_sig, str) or not hex_sig:
        raise SignerClientError("sign_failed")
    try:
        return bytes.fromhex(hex_sig)
    except ValueError as exc:
        raise SignerClientError("sign_failed") from exc


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
    """HMAC + one JSON-line. Never dials when the router token is missing.

    timestamp is unix now (short expiry on the signer). request_id is unique.
    Does not POST to Algorand.
    """
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
    return _parse_reply(raw)
