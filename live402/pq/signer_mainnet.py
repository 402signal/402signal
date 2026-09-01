"""MainNet-only 6PN client for 402signal-pq-signer-mainnet (pq-anchor/1).

ABSOLUTELY NO fallback to TestNet config:
  - never reads LIVE402_PQ_SIGNER_TOKEN
  - never dials 402signal-pq-signer.internal
  - never reads LIVE402_PQ_SIGNER_HOST / LIVE402_PQ_SIGNER_PORT

Uses ONLY:
  LIVE402_PQ_SIGNER_MAINNET_TOKEN
  LIVE402_PQ_SIGNER_MAINNET_HOST  (default 402signal-pq-signer-mainnet.internal)
  LIVE402_PQ_SIGNER_MAINNET_PORT  (default 9091)

Same request fields as TestNet pq-anchor/1. Never sends fee / sender /
receiver / amount / unsigned txn / Falcon SK. Reply is bound to
tree / root / origin / checkpoint plus expected MainNet identity.
Full SignedTxn semantic verify runs on the router before persist.

This module never loads a Falcon SK. No Algorand submit.
"""

from __future__ import annotations

import socket
import time
import uuid

from live402.pq import ORIGIN_MAINNET
from live402.pq import algo_anchor
from live402.pq import checkpoint as ckpt
from live402.pq import network as netcfg
from live402.pq.signer_client import (
    CANONICAL_VERSION,
    IPC_TIMEOUT,
    PQSIG_PRESENT,
    REPLY_KEYS,
    REQUEST_KEYS,
    SignerClientError,
    bind_reply,
    build_request,
    encode_request_line,
    parse_reply,
    require_signed_note,
)

TOKEN_ENV = netcfg.MAINNET_SIGNER_TOKEN_ENV
IPC_HOST_ENV = netcfg.MAINNET_SIGNER_HOST_ENV
IPC_PORT_ENV = netcfg.MAINNET_SIGNER_PORT_ENV
DEFAULT_IPC_HOST = netcfg.MAINNET_SIGNER_DEFAULT_HOST
DEFAULT_IPC_PORT = netcfg.MAINNET_SIGNER_DEFAULT_PORT
_FORBIDDEN_PORTS = frozenset({8080})
_MAX_LINE = 65536
# Documented merged signer identity. Not a secret.
SIGNER_APP = "402signal-pq-signer-mainnet"
SIGNER_MERGE_SHA = "a901ef7a"
SIGNER_REVIEWED_HEAD = "9798c38f"
SIGNER_PROTOCOL = "pq-anchor/1"


def mainnet_signer_token() -> str:
    """MainNet HMAC token only. Empty/unset never dials."""
    return (os_environ_get(TOKEN_ENV) or "").strip()


def os_environ_get(name: str) -> str:
    """Indirection so tests can prove TestNet env names are never read."""
    import os

    if name in {
        "LIVE402_PQ_SIGNER_TOKEN",
        "LIVE402_PQ_SIGNER_HOST",
        "LIVE402_PQ_SIGNER_PORT",
    }:
        raise SignerClientError("mainnet client must not read testnet signer env")
    return os.environ.get(name) or ""


def token_configured() -> bool:
    return bool(mainnet_signer_token())


def ipc_port(raw: str | int | None = None) -> int:
    if raw is None:
        text = (os_environ_get(IPC_PORT_ENV) or "").strip()
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
    explicit = (os_environ_get(IPC_HOST_ENV) or "").strip()
    if explicit:
        return explicit
    return DEFAULT_IPC_HOST


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


def bind_mainnet_reply(
    reply: dict,
    *,
    tree_size: int,
    root,
    origin: str,
    checkpoint: str,
) -> dict:
    """Fail closed unless reply matches tree/root and MainNet identity."""
    bound = bind_reply(reply, tree_size=tree_size, root=root)
    if origin != ORIGIN_MAINNET:
        raise SignerClientError("sign_failed")
    note = require_signed_note(checkpoint)
    try:
        parsed = ckpt.parse_signed_note(note)
        body = ckpt.parse_checkpoint_body(parsed["text"])
    except (ValueError, KeyError, TypeError) as exc:
        raise SignerClientError("sign_failed") from exc
    if str(body.get("origin") or "") != ORIGIN_MAINNET:
        raise SignerClientError("sign_failed")
    if int(body.get("tree_size") or -1) != int(tree_size):
        raise SignerClientError("sign_failed")
    return bound


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
    expected_address: str | None = None,
    params: dict | None = None,
) -> dict:
    """Dial the MainNet signer once. Never reads TestNet token/host/port.

    Token unset: never dial. Reply is bound then semantically verified.
    Does not POST. Does not persist.
    """
    secret = mainnet_signer_token()
    if not secret:
        raise SignerClientError("token unset")
    if origin != ORIGIN_MAINNET:
        raise SignerClientError("not mainnet origin")
    dest_host = (host or ipc_peer_host()).strip()
    if dest_host == "402signal-pq-signer.internal":
        raise SignerClientError("testnet signer host forbidden")
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
    if set(payload) != set(REQUEST_KEYS):
        raise SignerClientError("request keys")
    for forbidden in (
        "fee",
        "firstValid",
        "sender",
        "amount",
        "txn",
        "unsigned",
        "pk",
        "sk",
        "mnemonic",
    ):
        if forbidden in payload:
            raise SignerClientError("request keys")
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
    bind_mainnet_reply(
        data,
        tree_size=tree_size,
        root=root,
        origin=origin,
        checkpoint=checkpoint,
    )
    addr = (expected_address or algo_anchor.falcon_address_for(algo_anchor.MAINNET_NAME) or "").strip()
    verified = algo_anchor.validate_signed_txn(
        bytes(data["signed"]),
        expected_origin=origin,
        expected_size=int(tree_size),
        expected_root=root,
        expected_address=addr or None,
        expected_network=algo_anchor.MAINNET_NAME,
        params=params,
        require_canonical=True,
    )
    data["verified"] = verified
    return data


def request_sign(**kwargs) -> bytes:
    data = request_signed(**kwargs)
    return data["signed"]


def protocol_probe(*, host: str | None = None, port: int | None = None, timeout: float = 3.0) -> dict:
    """Prove the MainNet signer is reachable and speaks pq-anchor/1.

    Sends a well-formed request with an invalid HMAC so the signer must
    reject without creating an authorization. Never uses the TestNet
    token or host. Never persists. Output has no secrets.
    """
    dest_host = (host or ipc_peer_host()).strip()
    if dest_host == "402signal-pq-signer.internal":
        return {"reachable": False, "protocol": False, "error": "testnet_host_forbidden"}
    dest_port = ipc_port() if port is None else ipc_port(port)
    dummy = "00" * 32
    # Invalid checkpoint and HMAC on purpose: probe must not create auth.
    line = (
        '{"v":1,"origin":"%s","tree_size":1,"root":"%s",'
        '"consistency":[],"timestamp":1,"request_id":"preflight",'
        '"checkpoint":"unsigned","hmac":"%s"}\n'
        % (ORIGIN_MAINNET, dummy, dummy)
    )
    reachable = False
    protocol = False
    error = ""
    try:
        with socket.create_connection((dest_host, dest_port), timeout=timeout) as sock:
            reachable = True
            sock.sendall(line.encode("utf-8"))
            raw = _recv_line(sock, timeout)
        try:
            import json

            data = json.loads(raw)
        except Exception:
            error = "invalid_json"
            return {
                "reachable": True,
                "protocol": False,
                "error": error,
                "host": dest_host,
                "port": dest_port,
                "app": SIGNER_APP,
            }
        if not isinstance(data, dict):
            error = "invalid_json"
        elif data.get("ok") is True:
            # A success reply to an invalid HMAC is a protocol failure.
            error = "unexpected_ok"
        else:
            protocol = True
            error = str(data.get("error") or "rejected")
    except Exception as exc:
        error = type(exc).__name__
        if not reachable:
            error = "unreachable"
    return {
        "reachable": reachable,
        "protocol": protocol,
        "error": error,
        "host": dest_host,
        "port": dest_port,
        "app": SIGNER_APP,
        "merge_sha": SIGNER_MERGE_SHA,
        "reviewed_head": SIGNER_REVIEWED_HEAD,
        "canonical": CANONICAL_VERSION,
        "pqsig": PQSIG_PRESENT,
        "reply_keys": sorted(REPLY_KEYS),
    }
