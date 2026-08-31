"""PQ1 anchor worker on the HTTP app process.

Queue unsigned checkpoint requests. Falcon signing is the 6PN client
(pq-anchor/1). LIVE402_PQ_SIGNER_TOKEN unset: never dial, never sign.

Authorized vs confirmed are separate durable records:
  AUTHORIZED — signer returned a SignedTxn (request_id + blob persisted)
  CONFIRMED  — independently observed TestNet inclusion only

A signer reply advances AUTHORIZED only. last_anchor() / public status
read CONFIRMED only. should_build is vs CONFIRMED, so signed-but-unbroadcast
stays retryable. Same checkpoint is idempotent; a different checkpoint at
the same size is not authorized. send_forbidden remains the only send path.
This SHA has no function that POSTs a signed Falcon txn.
"""

from __future__ import annotations

import time
import uuid

from live402.pq import ANCHOR_SLA_LEAVES, ANCHOR_SLA_SECONDS, ORIGIN
from live402.pq import algo_anchor
from live402.pq import checkpoint as ckpt
from live402.pq import store

_queue: list[dict] = []

_PLACEHOLDER_TXID = frozenset({"", "your_txid", "placeholder", "txid", "none", "null"})


def last_authorized() -> dict:
    data = store.last_authorized_checkpoint()
    return {
        "size": int(data.get("size") or 0),
        "at": int(data.get("at") or 0),
        "request_id": str(data.get("request_id") or ""),
        "origin": str(data.get("origin") or ""),
        "root": str(data.get("root") or ""),
        "checkpoint": str(data.get("checkpoint") or ""),
        "signed": data.get("signed") if isinstance(data.get("signed"), (bytes, bytearray)) else b"",
    }


def last_confirmed() -> dict:
    data = store.last_confirmed_checkpoint()
    return {
        "size": int(data.get("size") or 0),
        "at": int(data.get("at") or 0),
        "txid": str(data.get("txid") or ""),
        "round": int(data.get("round") or 0),
        "root": str(data.get("root") or ""),
        "origin": str(data.get("origin") or ""),
    }


def last_anchor() -> dict:
    """Public latest anchor. CONFIRMED only. Authorized is never exposed here."""
    conf = last_confirmed()
    return {"size": conf["size"], "at": conf["at"]}


def public_anchor() -> dict | None:
    """Homepage / explorer CTA. None unless a real confirmed TestNet txn exists."""
    conf = last_confirmed()
    if conf["size"] < 1 or not conf["txid"]:
        return None
    return conf


def save_anchor(size: int, at: int) -> None:
    """Legacy. Writes AUTHORIZED-only {size, at}. Not confirmed.

    Old pq-log meta['anchor'] rows migrate to this meaning.
    """
    store.save_authorized_checkpoint(
        tree_size=int(size),
        origin=store.origin() or ORIGIN,
        root=b"",
        checkpoint="",
        request_id="",
        signed=b"",
        at=int(at),
    )


def should_build(now: int | None = None, tree_size: int | None = None) -> bool:
    """SLA vs last CONFIRMED size. Authorized/signed is not an anchor.

    15 min with ≥1 new leaf OR 1000 leaves, whichever first.
    If size is unchanged vs confirmed, return False without building.
    """
    current = store.size() if tree_size is None else int(tree_size)
    prev = last_confirmed()
    if current == prev["size"]:
        return False
    if current < prev["size"]:
        return False
    grown = current - prev["size"]
    if grown >= ANCHOR_SLA_LEAVES:
        return True
    when = int(now if now is not None else time.time())
    if grown >= 1 and (when - prev["at"]) >= ANCHOR_SLA_SECONDS:
        return True
    return False


def enqueue_unsigned(now: int | None = None) -> dict | None:
    """Queue a checkpoint request. Does not build a txn on idle."""
    if not should_build(now=now):
        return None
    size = store.size()
    root = store.root(size)
    item = {
        "origin": store.origin() or ORIGIN,
        "tree_size": size,
        "root": root.hex(),
        "queued_at": int(now if now is not None else time.time()),
    }
    _queue.append(item)
    return item


def queued() -> list[dict]:
    return list(_queue)


def clear_queue() -> None:
    _queue.clear()


def _recover_authorized(size: int, note: str, root_hex: str) -> dict | None:
    """Return stored SignedTxn for this size. Never replaces a different checkpoint."""
    del note, root_hex
    existing = store.authorized_at(size)
    if not existing or not existing.get("signed"):
        return None
    return {
        "tree_size": int(existing["tree_size"]),
        "signed": bytes(existing["signed"]),
        "request_id": existing.get("request_id") or "",
        "submitted": False,
        "status": "pending",
        "authorized": True,
        "confirmed": False,
    }


def process_one(signer_callback, sender: str, params: dict | None = None, now: int | None = None) -> dict | None:
    """Build one unsigned PaymentTxn, run an injected callback, do not submit."""
    if not _queue:
        return None
    item = _queue.pop(0)
    root = bytes.fromhex(item["root"])
    size = int(item["tree_size"])
    existing = store.authorized_at(size)
    if existing and existing.get("signed"):
        return None
    note = algo_anchor.encode_note(item["origin"], size, root)
    txn = algo_anchor.build_payment_txn(sender, note, params)
    if str(txn.get("gen") or "") == algo_anchor.MAINNET_GENESIS_ID:
        raise algo_anchor.AnchorError("not pq1 construction")
    pqsig = algo_anchor.isolated_sign(txn, signer_callback, pk=None)
    when = int(now if now is not None else time.time())
    store.save_authorized_checkpoint(
        tree_size=size,
        origin=item["origin"],
        root=root,
        checkpoint="",
        request_id="",
        signed=bytes(pqsig),
        at=when,
    )
    return {
        "tree_size": size,
        "note": note,
        "txn": txn,
        "pqsig": pqsig,
        "submitted": False,
        "status": "pending",
    }


def maybe_submit(
    signer_callback,
    sender: str | None = None,
    params: dict | None = None,
    now: int | None = None,
    send_fn=None,
    tree_size: int | None = None,
) -> dict | None:
    """6PN client only. Token unset: never dial. Never submit a Falcon txn.

    A signer reply persists AUTHORIZED (request_id + SignedTxn). Does not
    advance CONFIRMED. Same checkpoint recovers the stored blob (no re-dial).
    MainNet genesis is rejected. send_fn is ignored (no submit path).
    """
    del send_fn
    from live402.pq import signer_client

    if not signer_client.token_configured():
        return None
    p = dict(params) if isinstance(params, dict) else {}
    gen = str(p.get("genesisID") or p.get("genesis_id") or algo_anchor.TESTNET_GENESIS_ID)
    if gen == algo_anchor.MAINNET_GENESIS_ID or gen != algo_anchor.TESTNET_GENESIS_ID:
        return None
    size = store.size() if tree_size is None else int(tree_size)
    origin = store.origin() or ORIGIN
    root = store.root(size)
    note = store.checkpoint_at(size) or store.latest_checkpoint()
    if not note:
        if not should_build(now=now, tree_size=tree_size):
            return None
        return None
    try:
        ckpt.parse_signed_note(note)
    except ValueError:
        return None
    recovered = _recover_authorized(size, note, root.hex())
    if recovered:
        return recovered
    if not should_build(now=now, tree_size=tree_size):
        return None
    if size == last_confirmed()["size"]:
        return None
    prev = last_confirmed()
    consistency = [node.hex() for node in store.consistency_path(prev["size"], size)]
    request_id = uuid.uuid4().hex
    try:
        signed = signer_client.request_sign(
            origin=origin,
            tree_size=size,
            root=root,
            consistency=consistency,
            checkpoint=note,
            now=now,
            request_id=request_id,
        )
    except Exception:
        return None
    when = int(now if now is not None else time.time())
    stored = store.save_authorized_checkpoint(
        tree_size=size,
        origin=origin,
        root=root,
        checkpoint=note,
        request_id=request_id,
        signed=signed,
        at=when,
    )
    return {
        "tree_size": size,
        "signed": bytes(stored.get("signed") or signed),
        "request_id": stored.get("request_id") or request_id,
        "submitted": False,
        "status": "pending",
        "authorized": True,
        "confirmed": False,
    }


def confirm_testnet_anchor(
    *,
    tree_size: int,
    txid: str,
    confirmed_round: int,
    root,
    origin: str | None = None,
    at: int | None = None,
) -> dict:
    """Advance CONFIRMED only after an independently observed TestNet txn.

    Does not POST to algod. Rejects empty or placeholder txid. Signing
    success is not confirmation.
    """
    size = int(tree_size)
    if size < 1:
        raise algo_anchor.AnchorError("not independently confirmed")
    text = (txid or "").strip()
    low = text.lower()
    if low in _PLACEHOLDER_TXID or "placeholder" in low or text == "YOUR_TXID":
        raise algo_anchor.AnchorError("not independently confirmed")
    rnd = int(confirmed_round)
    if rnd < 1:
        raise algo_anchor.AnchorError("not independently confirmed")
    if isinstance(root, (bytes, bytearray)):
        root_b = bytes(root)
    else:
        try:
            root_b = bytes.fromhex(str(root or ""))
        except ValueError as exc:
            raise algo_anchor.AnchorError("not independently confirmed") from exc
    if len(root_b) != 32:
        raise algo_anchor.AnchorError("not independently confirmed")
    when = int(at if at is not None else time.time())
    return store.save_confirmed_checkpoint(
        tree_size=size,
        origin=origin or store.origin() or ORIGIN,
        root=root_b,
        txid=text,
        confirmed_round=rnd,
        at=when,
    )
