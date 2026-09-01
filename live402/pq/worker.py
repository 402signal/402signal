"""PQ1 anchor worker on the HTTP app process.

Queue unsigned checkpoint requests. Falcon signing is the 6PN client
(pq-anchor/1). LIVE402_PQ_SIGNER_TOKEN unset: never dial, never sign.

Authorized vs confirmed are separate durable records:
  AUTHORIZED — signer returned a SignedTxn (request_id + blob persisted)
  CONFIRMED  — persisted TestNet inclusion after independent fetch+verify

A signer reply advances AUTHORIZED only. last_anchor() / public status
read CONFIRMED only. should_build SLA is vs CONFIRMED, so the same
signed-but-unbroadcast checkpoint stays retryable. At most one
unconfirmed authorized checkpoint may exist: if last_authorized.size
> last_confirmed.size, do not authorize a newer tree size. Recover or
finish that exact authorization (same tree_size/origin/root/signed-note);
if it is already submitted, keep polling that txid. Only after it is
CONFIRMED may a newer size be authorized. Recover only if
size+origin+root+signed-note match. Mismatch fail-closes: no re-dial,
no old SignedTxn, no overwrite.

TestNet submit of the signer-approved SignedTxn is gated on
LIVE402_PQ_FALCON_BROADCAST=1. That env lives on this 402signal
router (default unset). 402security must GO before anyone sets it
to 1. The isolated signer never reads BROADCAST and never POSTs.
Signing or POST success never marks confirmed. Recovered records
with submitted=True and a real txid skip POST. tick() independently
GETs the TestNet indexer after a real submitted txid, then
confirm_testnet_anchor decodes, verifies, and persists last_confirmed.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

from live402.pq import ANCHOR_SLA_LEAVES, ANCHOR_SLA_SECONDS, ORIGIN
from live402.pq import algo_anchor
from live402.pq import checkpoint as ckpt
from live402.pq import store

_queue: list[dict] = []

_PLACEHOLDER_TXID = frozenset({"", "your_txid", "placeholder", "txid", "none", "null"})


class AuthorizedConflict(RuntimeError):
    """Stored authorized record does not match current size/origin/root/note."""


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
        "submitted": bool(data.get("submitted")),
        "txid": str(data.get("txid") or ""),
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
    text = str(conf.get("txid") or "").strip()
    low = text.lower()
    if conf["size"] < 1 or not text:
        return None
    if low in _PLACEHOLDER_TXID or "placeholder" in low or text == "YOUR_TXID":
        return None
    if int(conf.get("round") or 0) < 1:
        return None
    try:
        conf = dict(conf)
        conf["explorer"] = algo_anchor.testnet_explorer_url(text)
    except algo_anchor.AnchorError:
        return None
    return conf


def homepage_pq_html() -> str:
    """Homepage Latest confirmed line. Empty unless last_confirmed.size > 0 with a real txid."""
    from live402.pq import transparency as pq_view

    return pq_view.homepage_pq_html()


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


def in_flight_authorized() -> dict | None:
    """Authorized checkpoint strictly ahead of last_confirmed, if any."""
    auth = last_authorized()
    if int(auth.get("size") or 0) > int(last_confirmed().get("size") or 0):
        return auth
    return None


def should_build(now: int | None = None, tree_size: int | None = None) -> bool:
    """SLA vs last CONFIRMED size. Authorized/signed is not an anchor.

    15 min with ≥1 new leaf OR 1000 leaves, whichever first.
    If size is unchanged vs confirmed, return False without building.
    A newer size is not eligible while an older authorization is still
    unconfirmed (last_authorized.size > last_confirmed.size).
    """
    current = store.size() if tree_size is None else int(tree_size)
    prev = last_confirmed()
    if current == prev["size"]:
        return False
    if current < prev["size"]:
        return False
    inflight = in_flight_authorized()
    if inflight and current > int(inflight["size"]):
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


def _recover_authorized(size: int, origin: str, root_hex: str, note: str) -> dict | None:
    """Return persisted SignedTxn only if size, origin, root, and signed-note match.

    No record: None (caller may dial). Mismatch: AuthorizedConflict (fail closed).
    """
    existing = store.authorized_at(int(size))
    if not existing or not existing.get("signed"):
        return None
    want_root = str(root_hex or "").strip().lower()
    have_root = str(existing.get("root") or "").strip().lower()
    if (
        int(existing["tree_size"]) != int(size)
        or str(existing.get("origin") or "") != str(origin or "")
        or have_root != want_root
        or str(existing.get("checkpoint") or "") != str(note or "")
    ):
        raise AuthorizedConflict("authorized record does not match")
    txid = str(existing.get("txid") or "").strip()
    submitted = bool(existing.get("submitted")) and algo_anchor._looks_like_txid(txid)
    return {
        "tree_size": int(existing["tree_size"]),
        "signed": bytes(existing["signed"]),
        "request_id": existing.get("request_id") or "",
        "submitted": submitted,
        "txid": txid if submitted else "",
        "status": "authorized",
        "authorized": True,
        "confirmed": False,
        "origin": existing.get("origin") or "",
        "root": existing.get("root") or "",
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


def _persist_submitted(out: dict, txid: str) -> dict:
    """Record POST success on the authorized row. Does not write last_confirmed."""
    size = int(out.get("tree_size") or 0)
    existing = store.authorized_at(size) if size else None
    if not existing:
        return out
    stored = store.save_authorized_checkpoint(
        tree_size=size,
        origin=existing["origin"],
        root=existing["root"],
        checkpoint=existing["checkpoint"],
        request_id=existing.get("request_id") or out.get("request_id") or "",
        signed=existing["signed"],
        at=int(existing.get("at") or 0),
        submitted=True,
        txid=txid,
    )
    updated = dict(out)
    updated["submitted"] = True
    updated["txid"] = str(stored.get("txid") or txid)
    updated["confirmed"] = False
    return updated


def _maybe_broadcast(out: dict, *, send_fn, sender: str | None, params: dict) -> dict:
    """POST the signer-approved SignedTxn only if BROADCAST=1. Never confirms.

    If the authorized record already has submitted=True and a real txid,
    skip POST. POST success is persisted as submitted+txid only.
    """
    if not isinstance(out, dict):
        return out
    have_txid = str(out.get("txid") or "").strip()
    if out.get("submitted") and algo_anchor._looks_like_txid(have_txid):
        return out
    blob = out.get("signed")
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        return out
    addr = algo_anchor.falcon_address(sender)
    if not algo_anchor.submit_allowed(sender=addr, params=params):
        return out
    origin = str(out.get("origin") or "")
    size = int(out.get("tree_size") or 0)
    root = out.get("root")
    if not origin or size < 1:
        existing = store.authorized_at(int(out.get("tree_size") or 0)) if out.get("tree_size") else None
        if existing:
            origin = existing.get("origin") or store.origin() or ORIGIN
            size = int(existing.get("tree_size") or 0)
            root = existing.get("root")
    if not origin or size < 1:
        origin = store.origin() or ORIGIN
        size = int(out.get("tree_size") or store.size())
        root = store.root(size)
    try:
        algo_anchor.validate_signed_txn(
            bytes(blob),
            expected_origin=origin,
            expected_size=size,
            expected_root=root,
            expected_address=addr,
        )
    except algo_anchor.AnchorError:
        return out
    txid = algo_anchor.send_if_allowed(bytes(blob), send_fn=send_fn, sender=addr, params=params)
    if not txid:
        return out
    return _persist_submitted(out, txid)


def maybe_submit(
    signer_callback,
    sender: str | None = None,
    params: dict | None = None,
    now: int | None = None,
    send_fn=None,
    tree_size: int | None = None,
) -> dict | None:
    """6PN client only. Token unset: never dial.

    A signer reply persists AUTHORIZED (request_id + SignedTxn). Does not
    advance CONFIRMED. Exact size+origin+root+signed-note recovers the blob
    (no re-dial). Mismatch fail-closes. MainNet genesis is rejected.
    Router BROADCAST=1 may POST the recovered SignedTxn unless the
    authorized record already has submitted=True and a real txid.
    POST success is not confirmation.
    """
    from live402.pq import signer_client

    if not signer_client.token_configured():
        return None
    p = dict(params) if isinstance(params, dict) else {}
    gen = str(p.get("genesisID") or p.get("genesis_id") or algo_anchor.TESTNET_GENESIS_ID)
    if gen == algo_anchor.MAINNET_GENESIS_ID or gen != algo_anchor.TESTNET_GENESIS_ID:
        return None
    inflight = in_flight_authorized()
    if inflight:
        size = int(inflight["size"])
        origin = store.origin() or ORIGIN
        root = store.root(size)
        note = store.checkpoint_at(size) or str(inflight.get("checkpoint") or "")
        try:
            recovered = _recover_authorized(size, origin, root.hex(), note)
        except AuthorizedConflict:
            return None
        if recovered:
            return _maybe_broadcast(recovered, send_fn=send_fn, sender=sender, params=p)
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
    try:
        recovered = _recover_authorized(size, origin, root.hex(), note)
    except AuthorizedConflict:
        return None
    if recovered:
        return _maybe_broadcast(recovered, send_fn=send_fn, sender=sender, params=p)
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
    out = {
        "tree_size": size,
        "signed": bytes(stored.get("signed") or signed),
        "request_id": stored.get("request_id") or request_id,
        "submitted": False,
        "status": "authorized",
        "authorized": True,
        "confirmed": False,
        "origin": origin,
        "root": root.hex() if isinstance(root, (bytes, bytearray)) else str(root or ""),
    }
    return _maybe_broadcast(out, send_fn=send_fn, sender=sender, params=p)


def confirm_testnet_anchor(
    txid: str | None = None,
    *,
    fetch_fn=None,
    at: int | None = None,
    tree_size=None,
    confirmed_round=None,
    root=None,
    origin=None,
) -> dict:
    """Independently fetch, decode, verify, then persist last_confirmed.

    Caller txid is only a lookup key. Inclusion fields come from the
    fetched TestNet object. Signing success is not confirmation.
    Caller tree_size / confirmed_round / root / origin are ignored.
    """
    del tree_size, confirmed_round, root, origin
    text = (txid or "").strip()
    low = text.lower()
    if low in _PLACEHOLDER_TXID or "placeholder" in low or text == "YOUR_TXID":
        raise algo_anchor.AnchorError("invalid confirmed fields")
    fetched = algo_anchor.fetch_testnet_txn(text, fetch_fn=fetch_fn)
    if not fetched:
        raise algo_anchor.AnchorError("txn not fetched")
    decoded = algo_anchor.decode_chain_txn(fetched)
    auth = last_authorized()
    if int(auth.get("size") or 0) < 1 or not auth.get("root"):
        raise algo_anchor.AnchorError("no authorized checkpoint")
    want_root = auth["root"]
    try:
        want_root_b = bytes.fromhex(str(want_root))
    except ValueError as exc:
        raise algo_anchor.AnchorError("invalid authorized root") from exc
    verified = algo_anchor.verify_fetched_anchor(
        decoded,
        expected_origin=auth.get("origin") or store.origin() or ORIGIN,
        expected_size=int(auth["size"]),
        expected_root=want_root_b,
        expected_address=algo_anchor.falcon_address(),
        expected_txid=text,
    )
    when = int(at if at is not None else time.time())
    return store.save_confirmed_checkpoint(
        tree_size=int(verified["tree_size"]),
        origin=verified["origin"],
        root=verified["root"],
        txid=verified["txid"],
        confirmed_round=int(verified["confirmed_round"]),
        at=when,
    )


def maybe_confirm(fetch_fn=None, at: int | None = None) -> dict | None:
    """Confirm only after an authorized record has a real submitted txid.

    Independently GETs TestNet indexer, verify_fetched_anchor, then
    persist last_confirmed. Never uses the POST response as inclusion.
    Placeholder and MainNet genesis are rejected by confirm_testnet_anchor.
    """
    auth = last_authorized()
    txid = str(auth.get("txid") or "").strip()
    if not auth.get("submitted") or not algo_anchor._looks_like_txid(txid):
        return None
    conf = last_confirmed()
    if str(conf.get("txid") or "") == txid and int(conf.get("size") or 0) >= 1:
        return conf
    try:
        return confirm_testnet_anchor(txid, fetch_fn=fetch_fn, at=at)
    except algo_anchor.AnchorError:
        return None


_tick_lock = threading.Lock()
_tick_thread: threading.Thread | None = None
_tick_stop = threading.Event()
_TICK_DEFAULT_S = 5.0


def _tick_sleep_s() -> float:
    raw = (os.environ.get("LIVE402_PQ_TICK_S") or "").strip()
    try:
        n = float(raw) if raw else _TICK_DEFAULT_S
    except ValueError:
        n = _TICK_DEFAULT_S
    return max(1.0, min(30.0, n))


def start_worker() -> None:
    """Independent PQ tick loop. Fixture mode is network-free and does not start."""
    from live402 import fixtures

    if fixtures.fixture_mode():
        return
    global _tick_thread
    with _tick_lock:
        if _tick_thread is not None and _tick_thread.is_alive():
            return
        _tick_stop.clear()
        _tick_thread = threading.Thread(target=_tick_loop, name="pq-anchor", daemon=True)
        _tick_thread.start()


def stop_worker() -> None:
    _tick_stop.set()


def _tick_loop() -> None:
    from live402 import fixtures

    while not _tick_stop.wait(_tick_sleep_s()):
        if fixtures.fixture_mode():
            continue
        try:
            tick()
        except Exception:
            continue


def worker_running() -> bool:
    thread = _tick_thread
    return thread is not None and thread.is_alive()


def tick(
    signer_callback=None,
    sender: str | None = None,
    params: dict | None = None,
    now: int | None = None,
    send_fn=None,
    fetch_fn=None,
    tree_size: int | None = None,
) -> dict | None:
    """Existing PQ worker tick. Submit if needed, then independently confirm.

    Called from the independent pq-anchor thread (server.main → start_worker).
    Catalog trickle must not start a second copy. POST success is not confirmation.
    """
    try:
        maybe_submit(
            signer_callback,
            sender=sender,
            params=params,
            now=now,
            send_fn=send_fn,
            tree_size=tree_size,
        )
    except Exception:
        pass
    auth = last_authorized()
    txid = str(auth.get("txid") or "").strip()
    if not auth.get("submitted") or not algo_anchor._looks_like_txid(txid):
        return None
    conf = last_confirmed()
    if str(conf.get("txid") or "") == txid and int(conf.get("size") or 0) >= 1:
        return conf
    try:
        return confirm_testnet_anchor(txid, fetch_fn=fetch_fn, at=now)
    except algo_anchor.AnchorError:
        return None
