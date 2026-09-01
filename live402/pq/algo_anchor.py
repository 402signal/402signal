"""Algorand Falcon construction. Default path never broadcasts.

Note 84 bytes = ASCII "402sg/pq1:b" || 0x01 || SHA-256(UTF-8 origin) ||
uint64 BE tree_size || 32-byte RFC 6962 root.

Txn = PaymentTxn amount=0, receiver=sender, flat fee >= 3000 µALGO.
Falcon signing goes through the 6PN client (pq-anchor/1). This module
does not load a Falcon SK. send_forbidden() always raises.

TestNet submit of a signer-approved SignedTxn is gated on
LIVE402_PQ_FALCON_BROADCAST=1. That env lives on this 402signal
router (default unset). 402security must GO before anyone sets it
to 1. The isolated signer never reads BROADCAST and never POSTs.
Falcon SK must never live here. Fixture mode and CI never hit live
algod unless a send/fetch hook is injected. MainNet genesis has no
submit path.
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
from live402.pq.merkle import HASH_SIZE

NOTE_PREFIX = NOTE_FORMAT.encode("ascii")  # 11 bytes
NOTE_LEN = 84
MIN_FEE = 3000
MAX_FEE = 30000
ANCHOR_STATUSES = frozenset({"pending", "unavailable"})

# TestNet only for any submit path. Do not set MainNet.
TESTNET_NAME = "testnet"
TESTNET_GENESIS_ID = "testnet-v1.0"
MAINNET_GENESIS_ID = "mainnet-v1.0"
TESTNET_GENESIS_HASH = "SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI="
TESTNET_ALGOD_HOST = "testnet-api.algonode.cloud"
TESTNET_ALGOD_SEND_URL = "https://testnet-api.algonode.cloud/v2/transactions"
TESTNET_ALGOD_PENDING_URL = "https://testnet-api.algonode.cloud/v2/transactions/pending/"
TESTNET_INDEXER_HOST = "testnet-idx.algonode.cloud"
TESTNET_INDEXER_TXN_URL = "https://testnet-idx.algonode.cloud/v2/transactions/"
TESTNET_EXPLORER_TX_URL = "https://testnet.explorer.perawallet.app/tx/"
TESTNET_SEND_TIMEOUT = 8.0
TESTNET_FETCH_TIMEOUT = 8.0
USER_AGENT = "402Signal/0.1 (pq falcon testnet; no keys in logs)"
NETWORK_ENV = "LIVE402_PQ_FALCON_NETWORK"
BROADCAST_ENV = "LIVE402_PQ_FALCON_BROADCAST"
ADDRESS_ENV = "LIVE402_PQ_FALCON_ADDRESS"
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
    """Default send path. Always raises. Broadcast uses send_if_allowed."""
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

    Requires LIVE402_PQ_FALCON_NETWORK=testnet, router BROADCAST=1, a
    public address, and testnet-v1.0 genesis (MainNet genesis is
    rejected). A recovered SignedTxn does not need a live signer
    callback. Signer never reads BROADCAST.
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
) -> dict:
    """Semantic verify of a SignedTxn before any broadcast.

    TestNet pay-0 self-Falcon, expected note/origin/size/root, fee cap,
    no AuthAddr/rekey/close/group/lease/axfer/appcall. Does not parse a
    Falcon secret key. Does not POST.
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
    gen = str(txn.get("gen") or "")
    if gen == MAINNET_GENESIS_ID or gen != TESTNET_GENESIS_ID:
        raise AnchorError("not testnet genesis")
    fee = int(txn.get("fee") or 0)
    if fee < 1 or fee > MAX_FEE:
        raise AnchorError("fee out of range")
    addr = (expected_address or falcon_address() or "").strip()
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
    return {
        "origin": expected_origin,
        "tree_size": int(parsed["tree_size"]),
        "root": parsed["root"],
        "address": addr,
        "fee": fee,
    }


def send_if_allowed(signed: bytes, *, send_fn=None, sender: str | None = None, params: dict | None = None) -> str | None:
    """POST signer-approved SignedTxn bytes only when submit_allowed.

    send_fn is injected by tests. Fixture mode without send_fn never
    dials live algod. The pqsig marker is never treated as txn bytes.
    """
    if not isinstance(signed, (bytes, bytearray)) or not signed:
        return None
    blob = bytes(signed)
    if blob == PQSIG_MARKER.encode("utf-8") or blob == PQSIG_MARKER.encode("ascii"):
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


def _get_pinned(url: str, host: str, timeout: float) -> bytes | None:
    if not _pinned_https(url, host):
        return None
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
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


def fetch_testnet_txn(txid: str, fetch_fn=None):
    """Independently GET a confirmed TestNet txn. Never trusts caller fields.

    fetch_fn is injected by tests. Fixture mode without fetch_fn never
    dials live indexer/algod. MainNet hosts are not contacted.
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
) -> dict:
    """Fail closed unless the fetched txn matches PQ1 TestNet construction.

    Expected Falcon checkpoint is self-authorized: configured Falcon
    address == sender == receiver == authorizing account. Any nonempty
    AuthAddr (codec sgnr, REST auth-addr / authAddr) fails confirmation.
    """
    if not isinstance(decoded, dict):
        raise AnchorError("invalid chain object")
    pq_auth = decoded.get("pq_auth")
    if not isinstance(pq_auth, (bytes, bytearray)) or not pq_auth:
        raise AnchorError("falcon authorization missing")
    if bytes(pq_auth) == PQSIG_MARKER.encode("utf-8"):
        raise AnchorError("pqsig marker is not authorization")
    gen = str(decoded.get("genesis_id") or "")
    if gen == MAINNET_GENESIS_ID or gen != TESTNET_GENESIS_ID:
        raise AnchorError("not testnet genesis")
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
    if fee < 1 or fee > MAX_FEE:
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
    }
