"""In-process PQ1 anchor worker on the app process.

Queue unsigned checkpoint requests. Build a PaymentTxn only when the SLA
fires. Idle: do not even build an anchor if tree size is unchanged.

send_forbidden remains the default on the app process. maybe_submit()
may call algod send only when every TestNet gate passes (including
LIVE402_PQ_FALCON_BROADCAST=1 plus SK/callback). Otherwise it does not
build or send.

The isolated Falcon signer is a separate Fly process (shared-cpu-1x
256MB, ~$2/mo, Ross spend-GO). Scale stays 0 until 402security GOs.
402QA must not fly scale until that GO. Do not deploy from this module.
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


def maybe_submit(
    signer_callback,
    sender: str | None = None,
    params: dict | None = None,
    now: int | None = None,
    send_fn=None,
    tree_size: int | None = None,
) -> dict | None:
    """Build and send only if every TestNet gate passes. Else do neither.

    Gates: LIVE402_PQ_FALCON_NETWORK=testnet, BROADCAST=1, address set,
    isolated callback or loaded SK, SLA should_build, tree size changed,
    genesis testnet-v1.0 (MainNet rejected). send_fn is a mocked algod in
    tests; fixture mode never hits a live network.
    """
    addr = algo_anchor.falcon_address(sender)
    p = dict(params) if isinstance(params, dict) else {}
    gen = str(p.get("genesisID") or p.get("genesis_id") or algo_anchor.TESTNET_GENESIS_ID)
    if gen != algo_anchor.TESTNET_GENESIS_ID:
        return None
    p["genesisID"] = algo_anchor.TESTNET_GENESIS_ID
    if not (p.get("genesisHash") or p.get("genesis_hash")):
        p["genesisHash"] = algo_anchor.TESTNET_GENESIS_HASH
    if not algo_anchor.submit_allowed(signer_callback=signer_callback, sender=addr, params=p):
        return None
    if send_fn is None:
        from live402 import fixtures

        if fixtures.fixture_mode():
            return None
    if not should_build(now=now, tree_size=tree_size):
        return None
    size = store.size() if tree_size is None else int(tree_size)
    if size == last_anchor()["size"]:
        return None
    origin = store.origin() or ORIGIN
    root = store.root(size)
    note = algo_anchor.encode_note(origin, size, root)
    txn = algo_anchor.build_payment_txn(addr, note, p)
    if str(txn.get("gen") or "") != algo_anchor.TESTNET_GENESIS_ID:
        return None
    cb = signer_callback
    if not callable(cb):
        return None
    pqsig = algo_anchor.isolated_sign(txn, cb, pk=algo_anchor.current_falcon_sk())
    txid = algo_anchor.send_if_allowed(txn, pqsig, send_fn=send_fn, sender=addr)
    if not txid:
        return None
    when = int(now if now is not None else time.time())
    save_anchor(size, when)
    return {
        "tree_size": size,
        "note": note,
        "txn": txn,
        "pqsig": pqsig,
        "submitted": True,
        "status": "pending",
        "txid": txid,
    }
