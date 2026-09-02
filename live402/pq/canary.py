"""Durable one-shot MainNet canary. Worker / tick / boot never call this.

State machine (persisted on authorized_anchors):

  AUTHORIZED
      current production checkpoint -> MainNet signer -> SignedTxn
      -> semantic verify -> DURABLY persist exact blob
  SEND_INTENT / SEND_ATTEMPTED
      compute expected Algorand txid locally from the exact blob
      persist expected txid + blob + checkpoint identity + SEND_ATTEMPTED
      THEN one POST. After SEND_ATTEMPTED a restart must not POST again.
  SUBMITTED
      provider-returned txid equals locally expected txid
  CONFIRMED
      independent GET + local decode + semantic verify

Recovery reuses the exact stored SignedTxn. Never re-dial / re-sign
around an existing authorization unless origin, tree_size, root, and
signed-note are identical (same authorization policy). A mismatch
fail-closes for operator review.

After SEND_ATTEMPTED / a lost POST response: never ask the signer
for another txn, never advance fv/lv, never rebuild, never create
another auth, never auto-spend another fee, and never auto-POST
again. First query the locally expected txid. One-shot canary has
NO automatic second POST after SEND_ATTEMPTED.

Explicit human recovery (not implemented here; 402security must
approve later) may retransmit the EXACT SAME stored SignedTxn and
expected txid while the validity window is still open. If validity
has expired and the provider still has no matching txn, stop for
the operator. Provider txid must equal the local expected txid;
mismatch is a SECURITY FAILURE.

Both LIVE402_PQ_FALCON_MAINNET_BROADCAST=1 and
LIVE402_PQ_FALCON_MAINNET_CANARY=1 are required to send. Env flags
alone are not enough: SEND_ATTEMPTED is the durable one-shot latch.
Human GO is CONFIRM_MAINNET_CANARY=I_UNDERSTAND.
Default refuses to send. Fixture mode never dials live algod.
"""

from __future__ import annotations

import json
import os
import time
import uuid

from live402.pq import ORIGIN_MAINNET
from live402.pq import algo_anchor
from live402.pq import checkpoint as ckpt
from live402.pq import log_identity
from live402.pq import mainnet_params
from live402.pq import store
from live402.pq.signer_client import SignerClientError

STATE_AUTHORIZED = "AUTHORIZED"
STATE_SEND_INTENT = "SEND_INTENT"
STATE_SEND_ATTEMPTED = "SEND_ATTEMPTED"
STATE_SUBMITTED = "SUBMITTED"
STATE_CONFIRMED = "CONFIRMED"
TERMINAL_FAIL = "FAIL_CLOSED"

HUMAN_GO_ENV = "CONFIRM_MAINNET_CANARY"
HUMAN_GO_VALUE = "I_UNDERSTAND"

# Algorand ~2.8s/round * 1000 MaxTxnLife, plus operator slack.
VALIDITY_WINDOW_S = 3600

_POST_STATES = frozenset({STATE_SEND_ATTEMPTED, STATE_SUBMITTED, STATE_CONFIRMED})


class CanaryError(RuntimeError):
    pass


class CanarySecurityError(CanaryError):
    """Provider txid mismatch or incompatible re-authorization."""


def human_go_set() -> bool:
    return (os.environ.get(HUMAN_GO_ENV) or "").strip() == HUMAN_GO_VALUE


def _root_hex(root) -> str:
    if isinstance(root, (bytes, bytearray)):
        return bytes(root).hex()
    return str(root or "").strip().lower()


def _root_bytes(root):
    if isinstance(root, (bytes, bytearray)):
        return bytes(root)
    return bytes.fromhex(str(root or "").strip())


def same_authorization_policy(existing: dict, *, origin: str, tree_size: int, root, checkpoint: str) -> bool:
    """True only when stored auth is the exact same origin/tree/root/note."""
    if not existing or not existing.get("signed"):
        return False
    want_root = _root_hex(root)
    have_root = _root_hex(existing.get("root"))
    return (
        int(existing.get("tree_size") or existing.get("size") or 0) == int(tree_size)
        and str(existing.get("origin") or "") == str(origin or "")
        and have_root == want_root
        and str(existing.get("checkpoint") or "") == str(checkpoint or "")
    )


# Exact signer reject codes the operator canary may surface (allowlist).
_SIGNER_SURFACE_ERRORS = frozenset({"consistency_proof_required"})


def _authorized_consistency_floor(size: int) -> int:
    """Best known authorized floor for RFC 6962 consistency proofs.

    last_confirmed only advances after Falcon/on-chain confirm. With
    BROADCAST off that stays 0 after TREE_ADVANCE, so
    consistency_path(0, size) is empty and the signer fail-closes with
    consistency_proof_required when last_authorized is already at size-1.

    Floor is max(last_confirmed.size, last_authorized.tree_size if a
    signed blob exists). If still 0 and size > 1 (Falcon never confirmed;
    TREE_ADVANCE after a prior signer authorize the router may not have
    persisted), use size-1 so a single-step advance proves against signer
    last_authorized at size-1. Multi-step gaps without a confirmed or
    authorized base still need a real floor — do not invent proofs.
    """
    prev = int(store.last_confirmed_checkpoint().get("size") or 0)
    auth = store.last_authorized_checkpoint()
    if auth and auth.get("signed"):
        prev = max(prev, int(auth.get("tree_size") or auth.get("size") or 0))
    if prev == 0 and int(size) > 1:
        prev = int(size) - 1
    return prev


def _canary_from_signer_error(exc: SignerClientError) -> CanaryError:
    """Map allowlisted signer reject codes; everything else is unavailable."""
    code = str(exc).strip()
    if code in _SIGNER_SURFACE_ERRORS:
        return CanaryError(code)
    return CanaryError("signer unavailable")


def current_checkpoint_identity() -> dict:
    """Production checkpoint the canary may authorize. Fail closed if unsigned."""
    size = int(store.size() or 0)
    if size < 1:
        raise CanaryError("empty log")
    origin = store.origin() or log_identity.configured_origin()
    if origin != ORIGIN_MAINNET:
        raise CanaryError("not mainnet origin")
    root = store.root(size)
    note = store.checkpoint_at(size) or store.latest_checkpoint()
    if not note:
        raise CanaryError("no signed checkpoint")
    try:
        ckpt.parse_signed_note(note)
    except ValueError as exc:
        raise CanaryError("unsigned checkpoint") from exc
    prev = _authorized_consistency_floor(size)
    if prev >= size:
        # Same-size re-auth / first path: empty proof.
        consistency = []
    else:
        consistency = [node.hex() for node in store.consistency_path(prev, size)]
    return {
        "origin": origin,
        "tree_size": size,
        "root": root,
        "root_hex": root.hex(),
        "checkpoint": note,
        "consistency": consistency,
    }


def _resolve_trusted_params(params: dict | None, fetch_params_fn=None) -> dict:
    """Operator path fetches the pinned MainNet snapshot itself.

    Injected params are a test-only shortcut. Production CLI never
    passes them. Empty/missing params always fetch.
    """
    if fetch_params_fn is not None:
        return mainnet_params.fetch_trusted_mainnet_params(fetch_fn=fetch_params_fn)
    if isinstance(params, dict) and params.get("lastRound"):
        out = dict(params)
        out.setdefault("genesisID", algo_anchor.MAINNET_GENESIS_ID)
        out.setdefault("genesisHash", algo_anchor.MAINNET_GENESIS_HASH)
        out["require_canonical"] = True
        return out
    try:
        return mainnet_params.fetch_trusted_mainnet_params()
    except mainnet_params.ParamsError as exc:
        raise CanaryError("trusted mainnet params unavailable") from exc


def project_policy(
    *,
    now: int | None = None,
    params: dict | None = None,
    fetch_params_fn=None,
) -> dict:
    """Read-only projected HMAC policy. Never dials the signer. Never persists."""
    ident = current_checkpoint_identity()
    when = int(now if now is not None else time.time())
    p = _resolve_trusted_params(params, fetch_params_fn)
    note = algo_anchor.encode_note(ident["origin"], ident["tree_size"], ident["root"])
    draft = algo_anchor.build_mainnet_payment_txn(note, p)
    policy = algo_anchor.hmac_policy(algo_anchor.fee_policy_snapshot(p, unsigned=draft, now=when))
    return {
        "identity": ident,
        "params": p,
        "policy": policy,
        "draft": draft,
    }


def persist_authorized(
    *,
    tree_size: int,
    origin: str,
    root,
    checkpoint: str,
    request_id: str,
    signed: bytes,
    at: int,
    fee_policy: dict | None = None,
    fv: int = 0,
    lv: int = 0,
) -> dict:
    """DURABLY persist AUTHORIZED exact blob before any send eligibility."""
    policy = json.dumps(fee_policy) if isinstance(fee_policy, dict) else (fee_policy or "")
    stored = store.save_authorized_checkpoint(
        tree_size=int(tree_size),
        origin=origin,
        root=root,
        checkpoint=checkpoint,
        request_id=request_id,
        signed=bytes(signed),
        at=int(at),
        send_state=STATE_AUTHORIZED,
        fee_policy=policy,
        fv=int(fv or 0),
        lv=int(lv or 0),
    )
    return stored


def _existing_authorized_usable(existing: dict, ident: dict, *, now: int | None = None) -> dict:
    """Reuse fresh AUTHORIZED. Stale AUTHORIZED requires explicit discard."""
    if not existing or not existing.get("signed"):
        return {}
    if not same_authorization_policy(
        existing,
        origin=ident["origin"],
        tree_size=ident["tree_size"],
        root=ident["root"],
        checkpoint=ident["checkpoint"],
    ):
        raise CanarySecurityError("authorized record does not match")
    state = send_state_of(existing)
    if state in _POST_STATES:
        return existing
    try:
        algo_anchor.snapshot_fresh(_frozen_policy(existing), now=now)
    except algo_anchor.AnchorError as exc:
        raise CanaryError("stale authorized; explicit discard required") from exc
    return existing


def discard_authorized(row: dict | None = None) -> None:
    """Explicit operator discard of AUTHORIZED only. Never after SEND_ATTEMPTED."""
    auth = row or store.last_authorized_checkpoint()
    if not auth or not auth.get("signed"):
        return
    state = send_state_of(auth)
    if state in _POST_STATES:
        raise CanaryError("cannot discard after SEND_ATTEMPTED")
    try:
        store.discard_authorized_checkpoint(int(auth.get("tree_size") or auth.get("size") or 0))
    except store.StoreError as exc:
        raise CanaryError("cannot discard after SEND_ATTEMPTED") from exc


def authorize(
    *,
    now: int | None = None,
    request_id: str | None = None,
    params: dict | None = None,
    sign_fn=None,
    host: str | None = None,
    port: int | None = None,
    fetch_params_fn=None,
    observed_params: dict | None = None,
) -> dict:
    """Checkpoint -> fetch frozen policy -> MainNet signer -> verify -> persist.

    Operator path fetches the trusted snapshot itself. Reuses the exact
    stored SignedTxn when the authorization policy matches and the
    snapshot is still fresh. Stale AUTHORIZED fail-closes until the
    operator explicitly discards. Never re-dials around a different
    stored auth. Never silently modifies fee/fv/lv.
    """
    ident = current_checkpoint_identity()
    existing = store.authorized_at(ident["tree_size"])
    if existing and existing.get("signed"):
        return _existing_authorized_usable(existing, ident, now=now)
    from live402.pq import signer_mainnet

    rid = request_id or uuid.uuid4().hex
    when = int(now if now is not None else time.time())
    p = _resolve_trusted_params(params, fetch_params_fn)
    note = algo_anchor.encode_note(ident["origin"], ident["tree_size"], ident["root"])
    draft = algo_anchor.build_mainnet_payment_txn(note, p)
    policy = algo_anchor.hmac_policy(algo_anchor.fee_policy_snapshot(p, unsigned=draft, now=when))
    if observed_params is not None:
        try:
            algo_anchor.validate_observed_against_router_policy(
                policy, observed_params, now=when, unsigned=draft
            )
        except algo_anchor.AnchorError as exc:
            raise CanaryError("signer observation rejected") from exc
    verify_params = algo_anchor.params_from_fee_policy(policy)
    verify_params.setdefault("genesisID", algo_anchor.MAINNET_GENESIS_ID)
    verify_params.setdefault("genesisHash", algo_anchor.MAINNET_GENESIS_HASH)
    verify_params["require_canonical"] = True
    if sign_fn is not None:
        if not callable(sign_fn):
            raise CanaryError("invalid sign hook")
        signed = sign_fn(ident)
        reply = {"signed": bytes(signed), "verified": {}, "policy": policy}
    else:
        try:
            reply = signer_mainnet.request_signed(
                origin=ident["origin"],
                tree_size=ident["tree_size"],
                root=ident["root"],
                consistency=ident["consistency"],
                checkpoint=ident["checkpoint"],
                policy=policy,
                now=when,
                request_id=rid,
                host=host,
                port=port,
                params=verify_params,
            )
        except SignerClientError as exc:
            raise _canary_from_signer_error(exc) from exc
        signed = reply["signed"]
    verified = reply.get("verified")
    if not isinstance(verified, dict) or not verified.get("fee"):
        verified = algo_anchor.validate_signed_txn(
            bytes(signed),
            expected_origin=ident["origin"],
            expected_size=ident["tree_size"],
            expected_root=ident["root"],
            expected_address=algo_anchor.falcon_address_for(algo_anchor.MAINNET_NAME),
            expected_network=algo_anchor.MAINNET_NAME,
            params=verify_params,
            require_canonical=True,
        )
    if int(verified.get("fee") or 0) != int(policy["canonical_fee"]):
        raise CanarySecurityError("canonical fee mismatch")
    if int(verified.get("fv") or 0) != int(policy["fv"]):
        raise CanarySecurityError("canonical fv mismatch")
    if int(verified.get("lv") or 0) != int(policy["lv"]):
        raise CanarySecurityError("canonical lv mismatch")
    stored = persist_authorized(
        tree_size=ident["tree_size"],
        origin=ident["origin"],
        root=ident["root"],
        checkpoint=ident["checkpoint"],
        request_id=rid,
        signed=bytes(signed),
        at=when,
        fee_policy=policy,
        fv=int(verified.get("fv") or 0),
        lv=int(verified.get("lv") or 0),
    )
    return stored


def mark_send_attempted(row: dict, *, now: int | None = None) -> dict:
    """Persist expected txid + SEND_ATTEMPTED before the irreversible POST."""
    blob = bytes(row.get("signed") or b"")
    if not blob:
        raise CanaryError("not a signed pq1 txn")
    expected = str(row.get("expected_txid") or "").strip()
    if not expected:
        expected = algo_anchor.signed_txn_txid(blob)
    when = int(now if now is not None else time.time())
    return store.save_authorized_checkpoint(
        tree_size=int(row["tree_size"]),
        origin=row["origin"],
        root=row["root"],
        checkpoint=row["checkpoint"],
        request_id=row.get("request_id") or "",
        signed=blob,
        at=int(row.get("at") or when),
        send_state=STATE_SEND_ATTEMPTED,
        expected_txid=expected,
        fee_policy=row.get("fee_policy") or "",
        fv=int(row.get("fv") or 0),
        lv=int(row.get("lv") or 0),
        send_attempted_at=when,
    )


def mark_submitted(row: dict, txid: str) -> dict:
    expected = str(row.get("expected_txid") or "").strip()
    text = str(txid or "").strip()
    if not expected or text != expected:
        raise CanarySecurityError("provider txid mismatch")
    return store.save_authorized_checkpoint(
        tree_size=int(row["tree_size"]),
        origin=row["origin"],
        root=row["root"],
        checkpoint=row["checkpoint"],
        request_id=row.get("request_id") or "",
        signed=bytes(row["signed"]),
        at=int(row.get("at") or 0),
        submitted=True,
        txid=text,
        send_state=STATE_SUBMITTED,
        expected_txid=expected,
        fee_policy=row.get("fee_policy") or "",
        fv=int(row.get("fv") or 0),
        lv=int(row.get("lv") or 0),
        send_attempted_at=int(row.get("send_attempted_at") or 0),
    )


def send_state_of(row: dict | None) -> str:
    if not row:
        return ""
    state = str(row.get("send_state") or "").strip()
    if state:
        return state
    if row.get("submitted") and algo_anchor._looks_like_txid(str(row.get("txid") or "")):
        return STATE_SUBMITTED
    if row.get("signed"):
        return STATE_AUTHORIZED
    return ""


def _frozen_policy(row: dict) -> dict:
    raw = row.get("fee_policy") or ""
    if not raw:
        return {}
    try:
        policy = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return policy if isinstance(policy, dict) else {}


def _policy_params(row: dict, params: dict | None) -> dict:
    """Frozen stored snapshot wins. Caller params cannot move fee/fv/lv."""
    policy = _frozen_policy(row)
    if policy:
        out = algo_anchor.params_from_fee_policy(policy)
        out.setdefault("genesisID", algo_anchor.MAINNET_GENESIS_ID)
        out.setdefault("genesisHash", algo_anchor.MAINNET_GENESIS_HASH)
        out["require_canonical"] = True
        return out
    if isinstance(params, dict) and params:
        return dict(params)
    return {
        "genesisID": algo_anchor.MAINNET_GENESIS_ID,
        "genesisHash": algo_anchor.MAINNET_GENESIS_HASH,
        "require_canonical": True,
    }


def _reject_unsendable_policy(row: dict, *, now: int | None = None) -> None:
    """Fail closed on a stale or already-expired frozen snapshot before POST."""
    policy = _frozen_policy(row)
    try:
        algo_anchor.snapshot_fresh(policy, now=now)
    except algo_anchor.AnchorError as exc:
        raise CanaryError("stale snapshot") from exc
    last_round = int(policy.get("last_round") or 0)
    lv = int(row.get("lv") or policy.get("lv") or 0)
    if last_round >= 1 and lv < last_round:
        raise CanaryError("already expired; operator review")


def poll_expected(
    row: dict,
    *,
    fetch_fn=None,
    now: int | None = None,
) -> dict | None:
    """After SEND_ATTEMPTED: look up expected txid. Never POST."""
    expected = str(row.get("expected_txid") or "").strip()
    if not expected:
        return None
    fetched = algo_anchor.fetch_confirmed_txn(
        expected, network=algo_anchor.MAINNET_NAME, fetch_fn=fetch_fn
    )
    if not fetched:
        attempted = int(row.get("send_attempted_at") or row.get("at") or 0)
        when = int(now if now is not None else time.time())
        if attempted and when - attempted > VALIDITY_WINDOW_S:
            raise CanaryError("validity window expired; operator review")
        return None
    decoded = algo_anchor.decode_chain_txn(fetched)
    policy = _policy_params(row, None)
    stored_fee = 0
    frozen = _frozen_policy(row)
    if frozen.get("canonical_fee"):
        stored_fee = int(frozen["canonical_fee"])
    verified = algo_anchor.verify_fetched_anchor(
        decoded,
        expected_origin=row["origin"],
        expected_size=int(row["tree_size"]),
        expected_root=_root_bytes(row["root"]),
        expected_address=algo_anchor.falcon_address_for(algo_anchor.MAINNET_NAME),
        expected_txid=expected,
        expected_network=algo_anchor.MAINNET_NAME,
        expected_fee=stored_fee or None,
        expected_fv=int(row.get("fv") or frozen.get("fv") or 0) or None,
        expected_lv=int(row.get("lv") or frozen.get("lv") or 0) or None,
    )
    if str(verified.get("txid") or "") != expected:
        raise CanarySecurityError("provider txid mismatch")
    when = int(now if now is not None else time.time())
    store.save_authorized_checkpoint(
        tree_size=int(row["tree_size"]),
        origin=row["origin"],
        root=row["root"],
        checkpoint=row["checkpoint"],
        request_id=row.get("request_id") or "",
        signed=bytes(row["signed"]),
        at=int(row.get("at") or when),
        submitted=True,
        txid=expected,
        send_state=STATE_CONFIRMED,
        expected_txid=expected,
        fee_policy=row.get("fee_policy") or "",
        fv=int(row.get("fv") or 0),
        lv=int(row.get("lv") or 0),
        send_attempted_at=int(row.get("send_attempted_at") or 0),
    )
    confirmed = store.save_confirmed_checkpoint(
        tree_size=int(verified["tree_size"]),
        origin=verified["origin"],
        root=verified["root"],
        txid=verified["txid"],
        confirmed_round=int(verified["confirmed_round"]),
        at=when,
        network=str(verified.get("network") or algo_anchor.MAINNET_NAME),
        genesis_id=str(verified.get("genesis_id") or algo_anchor.MAINNET_GENESIS_ID),
    )
    del policy
    return confirmed


def send_durable(
    row: dict,
    *,
    authorize_human_canary: bool,
    params: dict | None = None,
    send_fn=None,
    fetch_fn=None,
    now: int | None = None,
    crash_before_post: bool = False,
    crash_after_send: bool = False,
) -> dict:
    """One-shot send. After SEND_ATTEMPTED, poll only. Never duplicate POST.

    crash_* flags are test hooks. Production callers leave them false.
    """
    state = send_state_of(row)
    if state == STATE_CONFIRMED:
        return store.last_confirmed_checkpoint()
    if state in {STATE_SEND_ATTEMPTED, STATE_SUBMITTED}:
        polled = poll_expected(row, fetch_fn=fetch_fn, now=now)
        if polled:
            return polled
        if state == STATE_SUBMITTED:
            raise CanaryError("submitted but not confirmed")
        raise CanaryError("send attempted; polling expected txid")
    if state not in {STATE_AUTHORIZED, STATE_SEND_INTENT, ""}:
        raise CanaryError("cannot send from state %s" % state)
    if authorize_human_canary is not True:
        raise CanaryError("canary not authorized")
    if not human_go_set():
        raise CanaryError("human go missing")
    if not algo_anchor.mainnet_canary_requested() or not algo_anchor.mainnet_broadcast_requested():
        raise CanaryError("mainnet dual flags off")
    blob = bytes(row.get("signed") or b"")
    _reject_unsendable_policy(row, now=now)
    p = _policy_params(row, params)
    p.setdefault("genesisID", algo_anchor.MAINNET_GENESIS_ID)
    p.setdefault("genesisHash", algo_anchor.MAINNET_GENESIS_HASH)
    attempted = mark_send_attempted(row, now=now)
    stored_blob = bytes(attempted.get("signed") or b"")
    if not stored_blob or stored_blob != blob:
        raise CanarySecurityError("stored blob mismatch")
    if crash_before_post:
        raise CanaryError("crash before post")
    try:
        txid = algo_anchor.submit_mainnet_canary(
            stored_blob,
            authorize_human_canary=True,
            sender=algo_anchor.falcon_address_for(algo_anchor.MAINNET_NAME),
            params=p,
            expected_origin=row["origin"],
            expected_size=int(row["tree_size"]),
            expected_root=_root_bytes(row["root"]),
            send_fn=send_fn,
        )
    except algo_anchor.AnchorError as exc:
        if crash_after_send:
            raise CanaryError("crash after send") from exc
        raise CanaryError("submit failed") from exc
    except Exception as exc:
        if crash_after_send:
            raise CanaryError("crash after send") from exc
        # Timeout / lost response: stay SEND_ATTEMPTED, do not POST again.
        raise CanaryError("submit incomplete") from exc
    if crash_after_send:
        raise CanaryError("crash after send")
    expected = str(attempted.get("expected_txid") or "")
    if str(txid or "") != expected:
        raise CanarySecurityError("provider txid mismatch")
    submitted = mark_submitted(attempted, str(txid))
    polled = poll_expected(submitted, fetch_fn=fetch_fn, now=now)
    return polled or submitted


def confirm(
    row: dict | None = None,
    *,
    fetch_fn=None,
    now: int | None = None,
) -> dict:
    auth = row or store.last_authorized_checkpoint()
    if not auth or not auth.get("signed"):
        raise CanaryError("no authorized checkpoint")
    out = poll_expected(auth, fetch_fn=fetch_fn, now=now)
    if not out:
        raise CanaryError("not confirmed")
    return out


def summary(row: dict | None = None, *, router_sha: str = "", params: dict | None = None) -> dict:
    """Human-readable public fields. Never includes secrets."""
    from live402.pq import signer_mainnet

    ident = None
    try:
        ident = current_checkpoint_identity()
    except CanaryError:
        ident = None
    auth = row or store.last_authorized_checkpoint()
    blob = bytes(auth.get("signed") or b"") if auth else b""
    fee = 0
    sender = algo_anchor.falcon_address_for(algo_anchor.MAINNET_NAME)
    expected = str((auth or {}).get("expected_txid") or "")
    fv = int((auth or {}).get("fv") or 0)
    lv = int((auth or {}).get("lv") or 0)
    if blob:
        try:
            verified = algo_anchor.validate_signed_txn(
                blob,
                expected_origin=(auth or {}).get("origin") or ORIGIN_MAINNET,
                expected_size=int((auth or {}).get("tree_size") or (auth or {}).get("size") or 0),
                expected_root=_root_bytes((auth or {}).get("root")),
                expected_address=sender,
                expected_network=algo_anchor.MAINNET_NAME,
                params=_policy_params(auth or {}, params),
                require_canonical=True,
            )
            fee = int(verified.get("fee") or 0)
            fv = int(verified.get("fv") or fv)
            lv = int(verified.get("lv") or lv)
            expected = expected or str(verified.get("txid") or "")
        except algo_anchor.AnchorError:
            fee = 0
    confirm = algo_anchor.confirm_provider(algo_anchor.MAINNET_NAME)
    return {
        "public_address": sender,
        "network": algo_anchor.MAINNET_NAME,
        "origin": (ident or {}).get("origin") or (auth or {}).get("origin") or "",
        "tree_size": int((ident or {}).get("tree_size") or (auth or {}).get("tree_size") or 0),
        "root": (ident or {}).get("root_hex") or str((auth or {}).get("root") or ""),
        "amount": 0,
        "sender": sender,
        "receiver": sender,
        "fee": fee,
        "fv": fv,
        "lv": lv,
        "expected_txid": expected,
        "send_state": send_state_of(auth),
        "router_sha": router_sha,
        "signer_sha": signer_mainnet.SIGNER_MERGE_SHA,
        "signer_reviewed_head": signer_mainnet.SIGNER_REVIEWED_HEAD,
        "signer_app": signer_mainnet.SIGNER_APP,
        "confirm_provider": confirm.get("host") or "",
        "confirm_org": confirm.get("org") or "",
        "independent_provider": bool(confirm.get("independent_of_submit")),
    }


def inspect(
    *,
    now: int | None = None,
    params: dict | None = None,
    fetch_params_fn=None,
    router_sha: str = "",
) -> dict:
    """Read-only operator inspect. Never authorizes. Never persists SignedTxn."""
    projected = None
    try:
        projected = project_policy(now=now, params=params, fetch_params_fn=fetch_params_fn)
    except (CanaryError, algo_anchor.AnchorError, mainnet_params.ParamsError):
        projected = None
    info = summary(None, router_sha=router_sha, params=params)
    if projected:
        info["projected_fee"] = projected["policy"]["canonical_fee"]
        info["projected_fv"] = projected["policy"]["fv"]
        info["projected_lv"] = projected["policy"]["lv"]
        info["projected_last_round"] = projected["policy"]["last_round"]
        info["projected_policy"] = projected["policy"]
    return {
        "state": send_state_of(store.last_authorized_checkpoint()),
        "authorized": None,
        "summary": info,
        "sent": False,
        "read_only": True,
        "projected": projected["policy"] if projected else {},
    }


def prepare(
    *,
    now: int | None = None,
    params: dict | None = None,
    sign_fn=None,
    fetch_params_fn=None,
    observed_params: dict | None = None,
    router_sha: str = "",
    host: str | None = None,
    port: int | None = None,
) -> dict:
    """Preflight already done by caller. Fetch policy, sign once, persist, no POST."""
    stored = authorize(
        now=now,
        params=params,
        sign_fn=sign_fn,
        fetch_params_fn=fetch_params_fn,
        observed_params=observed_params,
        host=host,
        port=port,
    )
    info = summary(stored, router_sha=router_sha, params=params)
    return {
        "state": send_state_of(stored),
        "authorized": stored,
        "summary": info,
        "sent": False,
        "expected_txid": info.get("expected_txid") or "",
    }


def send_persisted(
    *,
    authorize_human_canary: bool,
    params: dict | None = None,
    send_fn=None,
    fetch_fn=None,
    now: int | None = None,
    router_sha: str = "",
    crash_before_post: bool = False,
    crash_after_send: bool = False,
) -> dict:
    """Send an already-persisted AUTHORIZED blob. Never creates a fresh auth."""
    stored = store.last_authorized_checkpoint()
    if not stored or not stored.get("signed"):
        raise CanaryError("no AUTHORIZED blob")
    info = summary(stored, router_sha=router_sha, params=params)
    if authorize_human_canary is not True or not human_go_set():
        return {
            "state": send_state_of(stored),
            "authorized": stored,
            "summary": info,
            "sent": False,
            "refused": "human go missing or canary not authorized",
        }
    if not algo_anchor.mainnet_canary_requested() or not algo_anchor.mainnet_broadcast_requested():
        return {
            "state": send_state_of(stored),
            "authorized": stored,
            "summary": info,
            "sent": False,
            "refused": "mainnet dual flags off",
        }
    out = send_durable(
        stored,
        authorize_human_canary=True,
        params=params,
        send_fn=send_fn,
        fetch_fn=fetch_fn,
        now=now,
        crash_before_post=crash_before_post,
        crash_after_send=crash_after_send,
    )
    latest = store.authorized_at(int(stored["tree_size"]))
    return {
        "state": send_state_of(latest),
        "authorized": latest,
        "summary": summary(latest, router_sha=router_sha, params=params),
        "result": out,
        "sent": send_state_of(latest) in {STATE_SUBMITTED, STATE_CONFIRMED, STATE_SEND_ATTEMPTED},
    }


def run(
    *,
    authorize_human_canary: bool,
    params: dict | None = None,
    sign_fn=None,
    send_fn=None,
    fetch_fn=None,
    now: int | None = None,
    router_sha: str = "",
    crash_before_post: bool = False,
    crash_after_send: bool = False,
    fetch_params_fn=None,
) -> dict:
    """authorize -> persist -> summary. Send only with human GO + dual flags."""
    stored = authorize(now=now, params=params, sign_fn=sign_fn, fetch_params_fn=fetch_params_fn)
    info = summary(stored, router_sha=router_sha, params=params)
    if authorize_human_canary is not True or not human_go_set():
        return {
            "state": send_state_of(stored),
            "authorized": stored,
            "summary": info,
            "sent": False,
            "refused": "human go missing or canary not authorized",
        }
    if not algo_anchor.mainnet_canary_requested() or not algo_anchor.mainnet_broadcast_requested():
        return {
            "state": send_state_of(stored),
            "authorized": stored,
            "summary": info,
            "sent": False,
            "refused": "mainnet dual flags off",
        }
    out = send_durable(
        stored,
        authorize_human_canary=True,
        params=params,
        send_fn=send_fn,
        fetch_fn=fetch_fn,
        now=now,
        crash_before_post=crash_before_post,
        crash_after_send=crash_after_send,
    )
    return {
        "state": send_state_of(store.authorized_at(int(stored["tree_size"]))),
        "authorized": store.authorized_at(int(stored["tree_size"])),
        "summary": summary(store.authorized_at(int(stored["tree_size"])), router_sha=router_sha, params=params),
        "result": out,
        "sent": send_state_of(store.authorized_at(int(stored["tree_size"])))
        in {STATE_SUBMITTED, STATE_CONFIRMED, STATE_SEND_ATTEMPTED},
    }
