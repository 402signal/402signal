"""PQ1 anchor worker on the HTTP app process.

Queue unsigned checkpoint requests. Falcon signing is the 6PN client
(pq-anchor/1). LIVE402_PQ_SIGNER_TOKEN unset: never dial, never sign.

send_forbidden remains the only send path. This SHA has no function
that submits a signed Falcon txn. Signer constructs the PaymentTxn.
"""

from __future__ import annotations

import json
import time

from live402.pq import ANCHOR_SLA_LEAVES, ANCHOR_SLA_SECONDS, ORIGIN
from live402.pq import algo_anchor
from live402.pq import checkpoint as ckpt
from live402.pq import store

_queue: list[dict] = []


def last_anchor() -> dict:
    raw = store.meta_get("anchor")
    if not raw:
        return {"size": 0, "at": 0}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {"size": int(data.get("size") or 0), "at": int(data.get("at") or 0)}
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return {"size": 0, "at": 0}


def save_anchor(size: int, at: int) -> None:
    store.meta_set("anchor", json.dumps({"size": int(size), "at": int(at)}))


def should_build(now: int | None = None, tree_size: int | None = None) -> bool:
    """SLA: 15 min with ≥1 new leaf OR 1000 leaves, whichever first.

    If size is unchanged, return False without building anything.
    """
    current = store.size() if tree_size is None else int(tree_size)
    prev = last_anchor()
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


def process_one(signer_callback, sender: str, params: dict | None = None, now: int | None = None) -> dict | None:
    """Build one unsigned PaymentTxn, run an injected callback, do not submit."""
    if not _queue:
        return None
    item = _queue.pop(0)
    root = bytes.fromhex(item["root"])
    size = int(item["tree_size"])
    if size == last_anchor()["size"]:
        return None
    note = algo_anchor.encode_note(item["origin"], size, root)
    txn = algo_anchor.build_payment_txn(sender, note, params)
    if str(txn.get("gen") or "") == algo_anchor.MAINNET_GENESIS_ID:
        raise algo_anchor.AnchorError("not pq1 construction")
    pqsig = algo_anchor.isolated_sign(txn, signer_callback, pk=None)
    when = int(now if now is not None else time.time())
    save_anchor(size, when)
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

    MainNet genesis is rejected. send_fn is ignored (no submit path).
    Signer constructs the PaymentTxn; this process does not send fee,
    firstValid, sender, amount, or an unsigned txn.
    """
    del send_fn
    from live402.pq import signer_client

    if not signer_client.token_configured():
        return None
    p = dict(params) if isinstance(params, dict) else {}
    gen = str(p.get("genesisID") or p.get("genesis_id") or algo_anchor.TESTNET_GENESIS_ID)
    if gen == algo_anchor.MAINNET_GENESIS_ID or gen != algo_anchor.TESTNET_GENESIS_ID:
        return None
    if not should_build(now=now, tree_size=tree_size):
        return None
    size = store.size() if tree_size is None else int(tree_size)
    if size == last_anchor()["size"]:
        return None
    origin = store.origin() or ORIGIN
    root = store.root(size)
    prev = last_anchor()
    consistency = [node.hex() for node in store.consistency_path(prev["size"], size)]
    note = store.checkpoint_at(size) or store.latest_checkpoint()
    if not note:
        return None
    try:
        ckpt.parse_signed_note(note)
    except ValueError:
        return None
    try:
        signed = signer_client.request_sign(
            origin=origin,
            tree_size=size,
            root=root,
            consistency=consistency,
            checkpoint=note,
            now=now,
        )
    except Exception:
        return None
    when = int(now if now is not None else time.time())
    save_anchor(size, when)
    return {
        "tree_size": size,
        "signed": signed,
        "submitted": False,
        "status": "pending",
    }
