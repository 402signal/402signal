"""Algorand Falcon construction. Default path never broadcasts.

Note 84 bytes = ASCII "402sg/pq1:b" || 0x01 || SHA-256(UTF-8 origin) ||
uint64 BE tree_size || 32-byte RFC 6962 root.

Txn = PaymentTxn amount=0, receiver=sender, flat fee >= 3000 µALGO.
Isolated signer: unsigned txn in, pqsig out via callback.
send_forbidden() still raises. A separate TestNet helper may call algod
send only when every gate passes (testnet + BROADCAST=1 + address +
callback/SK + SLA + size change). MainNet genesis is rejected.
Falcon f1 is det Falcon-1024, not NIST FN-DSA, and is only for this txn
(not the Ed25519 log signature).
Never log, print, or commit LIVE402_PQ_FALCON_SK. 402dev never holds it.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from collections.abc import Callable
from urllib.parse import urlparse

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
TESTNET_ALGOD_HOST = "testnet-api.algonode.cloud"
TESTNET_ALGOD_SEND_URL = "https://testnet-api.algonode.cloud/v2/transactions"
TESTNET_SEND_TIMEOUT = 8.0
USER_AGENT = "402Signal/0.1 (pq falcon testnet; no keys in logs)"

NETWORK_ENV = "LIVE402_PQ_FALCON_NETWORK"
BROADCAST_ENV = "LIVE402_PQ_FALCON_BROADCAST"
ADDRESS_ENV = "LIVE402_PQ_FALCON_ADDRESS"
# Env name only. Never interpolate the value into logs or exceptions.
_SK_ENV = "LIVE402_PQ_FALCON_SK"

# det Falcon-1024 (scheme f1). Not NIST FN-DSA. Not an Ed25519 seed.
FALCON_SK_LEN = 2305

_falcon_sk: bytes | None = None


class AnchorError(ValueError):
    pass


class FalconSkError(ValueError):
    """LIVE402_PQ_FALCON_SK was set but could not be parsed. Do not generate a key."""


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
    """Default send path. Always raises. Broadcast uses send_if_allowed instead."""
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


def current_falcon_sk() -> bytes | None:
    """In-memory SK only. Callers must not log or serialize this."""
    return _falcon_sk


def configure_falcon_sk(raw: bytes | None) -> None:
    """Install or clear the in-memory Falcon SK. Never logs the bytes."""
    global _falcon_sk
    if raw is None:
        _falcon_sk = None
        return
    sk = bytes(raw)
    if len(sk) != FALCON_SK_LEN:
        raise FalconSkError("not falcon-1024")
    _falcon_sk = sk


def _parse_falcon_sk(raw: str) -> bytes:
    """Parse a det Falcon-1024 SK. Raises FalconSkError if malformed.

    Accepts 2305-byte hex (optional 0x) or standard base64. Rejects an
    Ed25519 32-byte seed (the log hex is a different key). Never include
    the secret in the exception message.
    """
    text = (raw or "").strip()
    if not text:
        raise FalconSkError("empty")
    if "BEGIN" in text and "PRIVATE KEY" in text:
        raise FalconSkError("not falcon-1024")
    hex_s = text.lower()
    if hex_s.startswith("0x"):
        hex_s = hex_s[2:]
    hex_s = "".join(hex_s.split())
    if hex_s and all(c in "0123456789abcdef" for c in hex_s):
        if len(hex_s) == 64:
            raise FalconSkError("not falcon-1024")
        if len(hex_s) != FALCON_SK_LEN * 2:
            raise FalconSkError("not falcon-1024")
        try:
            sk = bytes.fromhex(hex_s)
        except ValueError as exc:
            raise FalconSkError("not hex") from exc
        if len(sk) != FALCON_SK_LEN:
            raise FalconSkError("not falcon-1024")
        return sk
    compact = "".join(text.split())
    try:
        sk = base64.b64decode(compact, validate=True)
    except Exception as exc:
        raise FalconSkError("malformed") from exc
    if len(sk) != FALCON_SK_LEN:
        raise FalconSkError("not falcon-1024")
    return sk


def load_falcon_sk_from_env() -> bool:
    """Load LIVE402_PQ_FALCON_SK into memory. Never generate a key.

    Unset or blank: construction only (no SK).
    Malformed: fail closed — clear any SK, do not generate, keep serving.
    Success: keep bytes in memory only. Never log, print, or write the secret.
    """
    raw = os.environ.get(_SK_ENV)
    if raw is None or not str(raw).strip():
        return False
    try:
        sk = _parse_falcon_sk(raw)
    except Exception:
        configure_falcon_sk(None)
        sys.stderr.write("%s malformed; Falcon signing disabled\n" % _SK_ENV)
        return False
    configure_falcon_sk(sk)
    return True


def signer_material_present(signer_callback=None) -> bool:
    """Isolated callback and/or loaded Falcon SK. Does not reveal the SK."""
    return callable(signer_callback) or _falcon_sk is not None


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
    address, isolated callback or loaded SK, and testnet-v1.0 genesis
    (MainNet genesis is rejected). SLA / size-change are checked by the
    worker before it builds.
    """
    if configured_network() != TESTNET_NAME:
        return False
    if not broadcast_requested():
        return False
    if not falcon_address(sender):
        return False
    if not signer_material_present(signer_callback):
        return False
    gen = txn_genesis_id(txn, params)
    if gen == MAINNET_GENESIS_ID or gen != TESTNET_GENESIS_ID:
        return False
    return True


def encode_signed(unsigned_txn: dict, pqsig: bytes) -> bytes:
    """Wire blob for the TestNet send hook. Never includes a private key."""
    return algo_tx.encode_unsigned(unsigned_txn) + bytes(pqsig)


def send_if_allowed(unsigned_txn: dict, pqsig: bytes, *, send_fn=None, sender: str | None = None) -> str | None:
    """Call algod send only after submit_allowed. Default is still send_forbidden.

    send_fn is injected by tests (never a live network in CI). Production
    posts only to the pinned TestNet host. Fixture mode without send_fn
    refuses to send.
    """
    if not isinstance(unsigned_txn, dict):
        return None
    if not isinstance(pqsig, (bytes, bytearray)) or not pqsig:
        return None
    addr = sender or falcon_address()
    # Signing already produced pqsig; pass a dummy callback so the gate
    # does not re-require the isolated function object.
    if not submit_allowed(signer_callback=lambda _txn: None, sender=addr, txn=unsigned_txn):
        return None
    if str(unsigned_txn.get("gen") or "") != TESTNET_GENESIS_ID:
        return None
    blob = encode_signed(unsigned_txn, bytes(pqsig))
    if send_fn is not None:
        if not callable(send_fn):
            return None
        out = send_fn(blob)
        if out is None:
            return None
        return str(out)
    from live402 import fixtures

    if fixtures.fixture_mode():
        return None
    return _post_testnet(blob)


def _post_testnet(blob: bytes) -> str | None:
    """POST signed bytes to pinned TestNet algod. Never MainNet."""
    parsed = urlparse(TESTNET_ALGOD_SEND_URL)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != TESTNET_ALGOD_HOST:
        return None
    if parsed.username or parsed.password:
        return None
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
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
            raw = resp.read(256)
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    return text.strip('"')
