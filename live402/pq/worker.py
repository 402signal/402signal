"""In-process PQ1 anchor worker. Not a new Fly app.

Queue unsigned checkpoint requests. Build a PaymentTxn only when the SLA
fires. Idle: do not even build an anchor if tree size is unchanged.
Never call algod send. Ross must approve extra machines later.
"""

from __future__ import annotations

import json
import time

from live402.pq import ANCHOR_SLA_LEAVES, ANCHOR_SLA_SECONDS, ORIGIN
from live402.pq import algo_anchor
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
    """Build one unsigned PaymentTxn, run the isolated callback, do not submit."""
    if not _queue:
        return None
    item = _queue.pop(0)
    root = bytes.fromhex(item["root"])
    size = int(item["tree_size"])
    if size == last_anchor()["size"]:
        return None
    note = algo_anchor.encode_note(item["origin"], size, root)
    txn = algo_anchor.build_payment_txn(sender, note, params)
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
