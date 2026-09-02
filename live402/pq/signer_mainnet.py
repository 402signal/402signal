"""MainNet-only 6PN client for 402signal-pq-signer-mainnet (pq-anchor/2).

ABSOLUTELY NO fallback to TestNet config:
  - never reads LIVE402_PQ_SIGNER_TOKEN
  - never dials 402signal-pq-signer.internal
  - never reads LIVE402_PQ_SIGNER_HOST / LIVE402_PQ_SIGNER_PORT

Uses ONLY:
  LIVE402_PQ_SIGNER_MAINNET_TOKEN
  LIVE402_PQ_SIGNER_MAINNET_HOST  (default 402signal-pq-signer-mainnet.internal)
  LIVE402_PQ_SIGNER_MAINNET_PORT  (default 9091)

pq-anchor/2 HMAC-binds the narrow frozen policy by flattening those
fields into the same sorted k=v MAC as the Go signer (last_round,
min_fee, fee_per_byte, fv, lv, canonical_fee, snapshot_at, size_rule,
size_version=1). Never sends fee / sender /
receiver / amount / unsigned txn / Falcon SK as top-level keys.
Reply is bound to tree / root / origin / checkpoint plus expected
MainNet identity. Full SignedTxn semantic verify runs on the router
before persist and requires exact equality with the HMAC-bound policy.

Residual (Ross-only): the response SignedTxn bytes themselves are
not yet response-MAC authenticated. Do not weaken parse_reply or
bind_mainnet_reply. Do not invent Falcon crypto. Official native
Falcon verify, or a signer response-MAC over protocol version,
request_id, origin, tree size, root, checkpoint/policy digest, and
SHA-256 of the exact SignedTxn bytes, is required before treating
IPC bytes as authenticated provenance.

This module never loads a Falcon SK. No Algorand submit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import time
import uuid

from live402.pq import ORIGIN_MAINNET
from live402.pq import algo_anchor
from live402.pq import checkpoint as ckpt
from live402.pq import network as netcfg
from live402.pq.signer_client import (
    IPC_TIMEOUT,
    PQSIG_PRESENT,
    REPLY_KEYS,
    SignerClientError,
    bind_reply,
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
SIGNER_MERGE_SHA = "7e58e39817dce9b74c387ffe3a08536f804dcd05"
SIGNER_REVIEWED_HEAD = "6d2480ce5a53c9b7dd574a01c257b4faa2f8dac9"
SIGNER_PROTOCOL = "pq-anchor/2"
REQUEST_VERSION = 2
# Narrow HMAC-bound policy. Flattened into the MAC. Do not add
# arbitrary txn fields. size_version is required and must be the
# JSON string "1" on the wire (Go policy.Snapshot.SizeVersion is string).
HMAC_SIZE_VERSION = "1"
HMAC_SIZE_RULE = "deterministic_falcon_envelope_estimate"
POLICY_KEYS = (
    "canonical_fee",
    "fee_per_byte",
    "fv",
    "last_round",
    "lv",
    "min_fee",
    "size_rule",
    "size_version",
    "snapshot_at",
)
# Identity keys stay in the JSON request. Policy keys are nested in
# the JSON `policy` object but flattened into the HMAC byte string.
IDENTITY_CANONICAL_KEYS = (
    "checkpoint",
    "consistency",
    "origin",
    "request_id",
    "root",
    "timestamp",
    "tree_size",
    "v",
)
CANONICAL_KEYS = tuple(sorted(IDENTITY_CANONICAL_KEYS + POLICY_KEYS))
REQUEST_KEYS = (
    "v",
    "origin",
    "tree_size",
    "root",
    "consistency",
    "timestamp",
    "request_id",
    "checkpoint",
    "policy",
    "hmac",
)
# Preferred exact HMAC rejection from the reviewed private signer
# (valid pq-anchor/2 shape + invalid HMAC). Allowlist is fail-closed.
EXPECTED_HMAC_ERROR = "hmac"
# Reviewed private-signer wire: only exact error="hmac" proves the protocol.
# Alternate strings are historical and must FAIL the probe.
EXPECTED_HMAC_ERRORS = frozenset({"hmac"})
# Fail-closed signer reject the MainNet canary may surface (allowlist).
SURFACE_ERRORS = frozenset({"consistency_proof_required"})


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


def narrow_policy(policy: dict | None) -> dict:
    """Exact HMAC-bound policy object. Unknown extra keys are dropped."""
    if not isinstance(policy, dict):
        raise SignerClientError("policy required")
    out = {}
    for key in POLICY_KEYS:
        if key not in policy or policy.get(key) in (None, ""):
            raise SignerClientError("policy field missing")
        out[key] = policy[key]
    try:
        out["last_round"] = int(out["last_round"])
        out["min_fee"] = int(out["min_fee"])
        out["fee_per_byte"] = int(out["fee_per_byte"])
        out["fv"] = int(out["fv"])
        out["lv"] = int(out["lv"])
        out["canonical_fee"] = int(out["canonical_fee"])
        out["snapshot_at"] = int(out["snapshot_at"])
        out["size_rule"] = str(out["size_rule"])
    except (TypeError, ValueError) as exc:
        raise SignerClientError("policy field missing") from exc
    # Reject int 1: Go json.Unmarshal into string SizeVersion fails on number.
    # Do not coerce int→str. Emit/accept only JSON string "1".
    if not isinstance(out["size_version"], str) or out["size_version"] != HMAC_SIZE_VERSION:
        raise SignerClientError("policy field missing")
    if out["last_round"] < 1 or out["fv"] < 1 or out["lv"] < 1:
        raise SignerClientError("policy field missing")
    if out["min_fee"] < 1 or out["canonical_fee"] < 1:
        raise SignerClientError("policy field missing")
    if out["fee_per_byte"] < 0 or out["snapshot_at"] < 1:
        raise SignerClientError("policy field missing")
    if out["size_rule"] != HMAC_SIZE_RULE:
        raise SignerClientError("policy field missing")
    return {key: out[key] for key in POLICY_KEYS}


def flatten_policy_fields(policy: dict) -> dict:
    """Policy fields as decimal/string MAC values. No nested policy= blob."""
    bound = narrow_policy(policy)
    return {
        "canonical_fee": str(int(bound["canonical_fee"])),
        "fee_per_byte": str(int(bound["fee_per_byte"])),
        "fv": str(int(bound["fv"])),
        "last_round": str(int(bound["last_round"])),
        "lv": str(int(bound["lv"])),
        "min_fee": str(int(bound["min_fee"])),
        "size_rule": str(bound["size_rule"]),
        "size_version": str(bound["size_version"]),
        "snapshot_at": str(int(bound["snapshot_at"])),
    }


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
    policy: dict,
    v: int = REQUEST_VERSION,
) -> bytes:
    """pq-anchor/2 MAC input. Identity + flattened policy, sorted k=v.

    Matches the Go signer: no nested policy=… blob. size_version=1 is
    required. Field order is sorted(CANONICAL_KEYS).
    """
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
        "v": SIGNER_PROTOCOL,
    }
    fields.update(flatten_policy_fields(policy))
    if set(fields) != set(CANONICAL_KEYS):
        raise SignerClientError("request keys")
    parts = [SIGNER_PROTOCOL]
    for key in CANONICAL_KEYS:
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
    policy: dict,
    token: str | None = None,
) -> dict:
    """Exactly REQUEST_KEYS. HMAC covers identity + narrow policy."""
    secret = token if token is not None else mainnet_signer_token()
    if not secret:
        raise SignerClientError("token unset")
    note = require_signed_note(checkpoint)
    nodes = [] if consistency is None else list(consistency)
    hex_nodes = [_hex_node(n) for n in nodes]
    root_hex = _hex_node(root)
    bound = narrow_policy(policy)
    body = canonical_bytes(
        origin=origin,
        tree_size=int(tree_size),
        root=root_hex,
        consistency=hex_nodes,
        timestamp=int(timestamp),
        request_id=request_id,
        checkpoint=note,
        policy=bound,
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
        "policy": bound,
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
        "checkpoint_body",
    }
    if set(payload) & forbidden:
        raise SignerClientError("request keys")
    require_signed_note(str(payload.get("checkpoint") or ""))
    narrow_policy(payload.get("policy"))
    ordered = {key: payload[key] for key in REQUEST_KEYS}
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=True)


def hmac_error_expected(error: str) -> bool:
    """True only for the reviewed HMAC reject wire: error exactly "hmac"."""
    return str(error or "") == EXPECTED_HMAC_ERROR


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
    policy: dict,
    now: int | None = None,
    request_id: str | None = None,
    host: str | None = None,
    port: int | None = None,
    timeout: float = IPC_TIMEOUT,
    expected_address: str | None = None,
    params: dict | None = None,
) -> dict:
    """Dial the MainNet signer once. Never reads TestNet token/host/port.

    Token unset: never dial. Policy is HMAC-bound. Reply is bound then
    semantically verified against the exact frozen policy. Does not POST.
    Does not persist.
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
    bound = narrow_policy(policy)
    payload = build_request(
        origin=origin,
        tree_size=tree_size,
        root=root,
        consistency=consistency,
        timestamp=ts,
        request_id=rid,
        checkpoint=checkpoint,
        policy=bound,
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
    try:
        rejected = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        rejected = None
    if isinstance(rejected, dict) and rejected.get("ok") is False:
        error = str(rejected.get("error") or "").strip()
        if error in SURFACE_ERRORS:
            raise SignerClientError(error)
    data = parse_reply(raw)
    # Identity bind only. SignedTxn bytes are not response-MAC authenticated.
    bind_mainnet_reply(
        data,
        tree_size=tree_size,
        root=root,
        origin=origin,
        checkpoint=checkpoint,
    )
    addr = (expected_address or algo_anchor.falcon_address_for(algo_anchor.MAINNET_NAME) or "").strip()
    verify_params = algo_anchor.params_from_fee_policy(bound)
    verify_params.setdefault("genesisID", algo_anchor.MAINNET_GENESIS_ID)
    verify_params.setdefault("genesisHash", algo_anchor.MAINNET_GENESIS_HASH)
    verify_params["require_canonical"] = True
    if isinstance(params, dict) and params:
        # Injected params cannot move fee/fv/lv off the HMAC-bound policy.
        verify_params = algo_anchor.params_from_fee_policy(bound)
        verify_params.setdefault("genesisID", algo_anchor.MAINNET_GENESIS_ID)
        verify_params.setdefault("genesisHash", algo_anchor.MAINNET_GENESIS_HASH)
        verify_params["require_canonical"] = True
    verified = algo_anchor.validate_signed_txn(
        bytes(data["signed"]),
        expected_origin=origin,
        expected_size=int(tree_size),
        expected_root=root,
        expected_address=addr or None,
        expected_network=algo_anchor.MAINNET_NAME,
        params=verify_params,
        require_canonical=True,
    )
    if int(verified.get("fee") or 0) != int(bound["canonical_fee"]):
        raise SignerClientError("policy mismatch")
    if int(verified.get("fv") or 0) != int(bound["fv"]):
        raise SignerClientError("policy mismatch")
    if int(verified.get("lv") or 0) != int(bound["lv"]):
        raise SignerClientError("policy mismatch")
    data["verified"] = verified
    data["policy"] = bound
    return data


def request_sign(**kwargs) -> bytes:
    data = request_signed(**kwargs)
    return data["signed"]


def protocol_probe(*, host: str | None = None, port: int | None = None, timeout: float = 3.0) -> dict:
    """Prove the MainNet signer is reachable and speaks pq-anchor/2.

    Sends a well-formed pq-anchor/2 request (valid shape + dummy policy)
    with an invalid HMAC so the signer must reject without creating an
    authorization. Prefers the exact reviewed HMAC rejection
    (`{"ok":false,"error":"hmac"}`). Never uses the TestNet token or
    host. Never persists. Output has no secrets.
    """
    dest_host = (host or ipc_peer_host()).strip()
    if dest_host == "402signal-pq-signer.internal":
        return {"reachable": False, "protocol": False, "error": "testnet_host_forbidden", "hmac_rejected": False}
    dest_port = ipc_port() if port is None else ipc_port(port)
    dummy = "00" * 32
    dummy_policy = {
        "canonical_fee": 3000,
        "fee_per_byte": 0,
        "fv": 1,
        "last_round": 1,
        "lv": 1001,
        "min_fee": 1000,
        "size_rule": HMAC_SIZE_RULE,
        "size_version": HMAC_SIZE_VERSION,
        "snapshot_at": 1,
    }
    # Valid shape, invalid HMAC: probe must not create auth.
    line = (
        json.dumps(
            {
                "v": REQUEST_VERSION,
                "origin": ORIGIN_MAINNET,
                "tree_size": 1,
                "root": dummy,
                "consistency": [],
                "timestamp": 1,
                "request_id": "preflight",
                "checkpoint": "unsigned",
                "policy": dummy_policy,
                "hmac": dummy,
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    reachable = False
    protocol = False
    hmac_rejected = False
    error = ""
    try:
        with socket.create_connection((dest_host, dest_port), timeout=timeout) as sock:
            reachable = True
            sock.sendall(line.encode("utf-8"))
            raw = _recv_line(sock, timeout)
        try:
            data = json.loads(raw)
        except Exception:
            error = "invalid_json"
            return {
                "reachable": True,
                "protocol": False,
                "hmac_rejected": False,
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
        elif any(k in data for k in ("signed", "pqsig", "SignedTxn")):
            # Invalid-HMAC replies must never carry authorization material.
            error = "unexpected_signed"
        else:
            error = str(data.get("error") or "rejected")
            hmac_rejected = hmac_error_expected(error)
            protocol = bool(
                data.get("ok") is False
                and hmac_rejected
                and error == EXPECTED_HMAC_ERROR
            )
    except Exception as exc:
        error = type(exc).__name__
        if not reachable:
            error = "unreachable"
    return {
        "reachable": reachable,
        "protocol": protocol,
        "hmac_rejected": hmac_rejected,
        "error": error,
        "expected_hmac_error": EXPECTED_HMAC_ERROR,
        "host": dest_host,
        "port": dest_port,
        "app": SIGNER_APP,
        "merge_sha": SIGNER_MERGE_SHA,
        "reviewed_head": SIGNER_REVIEWED_HEAD,
        "canonical": SIGNER_PROTOCOL,
        "pqsig": PQSIG_PRESENT,
        "reply_keys": sorted(REPLY_KEYS),
    }
