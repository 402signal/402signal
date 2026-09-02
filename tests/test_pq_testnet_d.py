"""Gated TestNet submit + independent last_confirmed verifier. No live network."""

from __future__ import annotations

import base64
import inspect
import json
import os
import socket
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import catalog, payment
from live402.pq import ORIGIN, algo_anchor, store, worker
from live402.pq import checkpoint as ckpt
from live402.server import Handler


_TOKEN = "vector-token"
_TXID = "B" * 52
_SIG_NOTE = "%s\n%s %s %s\n" % (
    "402signal.com/pq/log\n1\nAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n",
    ckpt.EMDASH,
    "402signal.com/pq/log",
    base64.b64encode(b"\x00" * 4 + b"\x22" * 64).decode("ascii"),
)
_SIGNED = b"STXN-authorized-A" + bytes(range(24))


def _pq1_signed(size, root, addr=None):
    from live402 import algo_tx

    addr = addr or payment.DEFAULT_PAYTO_ALGORAND
    note = algo_anchor.encode_note(ORIGIN, int(size), bytes(root))
    gh = base64.b64decode(algo_anchor.TESTNET_GENESIS_HASH)
    txn = algo_tx.pay_txn(
        addr, addr, 0, 3000, 1, 1001, algo_anchor.TESTNET_GENESIS_ID, gh, note=note
    )
    return algo_tx.msgpack_encode(
        {"pqsig": {"pk": _FALCON_PK, "sch": "f1", "sig": _FALCON_AUTH, "slt": 0}, "txn": txn}
    )
_FALCON_AUTH = b"FALCON-PQ-AUTH" + bytes(range(48))
_FALCON_PK = b"FALCON-PK" + bytes(range(24))


def _indexer_pqsig(*, scheme="f1", salt=0, pk=_FALCON_PK, sig=_FALCON_AUTH):
    env = {"scheme": scheme}
    if salt is not None:
        env["salt"] = salt
    if pk is not None:
        env["public-key"] = base64.b64encode(bytes(pk)).decode("ascii") if isinstance(pk, (bytes, bytearray)) else pk
    if sig is not None:
        env["signature"] = base64.b64encode(bytes(sig)).decode("ascii") if isinstance(sig, (bytes, bytearray)) else sig
    return env


def _codec_pqsig(*, sch="f1", slt=0, pk=_FALCON_PK, sig=_FALCON_AUTH):
    env = {"sch": sch}
    if slt is not None:
        env["slt"] = slt
    if pk is not None:
        env["pk"] = base64.b64encode(bytes(pk)).decode("ascii") if isinstance(pk, (bytes, bytearray)) else pk
    if sig is not None:
        env["sig"] = base64.b64encode(bytes(sig)).decode("ascii") if isinstance(sig, (bytes, bytearray)) else sig
    return env


def _reply(signed, tree_size=1, root=None):
    return json.dumps(
        {
            "ok": True,
            "tree_size": int(tree_size),
            "root": root or "00" * 32,
            "pqsig": "present",
            "signed": signed.hex(),
        },
        separators=(",", ":"),
    )


def _signed_note(size, root):
    body = ckpt.checkpoint_body(ORIGIN, int(size), bytes(root))
    sig = base64.b64encode(b"\x00" * 4 + b"\x22" * 64).decode("ascii")
    return "%s\n%s %s %s\n" % (body, ckpt.EMDASH, ORIGIN, sig)


def _indexer_payload(
    *,
    txid=_TXID,
    sender=None,
    receiver=None,
    amount=0,
    fee=3000,
    note=None,
    genesis=algo_anchor.TESTNET_GENESIS_ID,
    confirmed_round=99,
    tx_type="pay",
    close="",
    rekey="",
    group="",
    lease="",
    pq_auth=_FALCON_AUTH,
    extra=None,
    signature=None,
    sig_type="pqsig",
):
    addr = sender or payment.DEFAULT_PAYTO_ALGORAND
    rcv = receiver if receiver is not None else addr
    if note is None:
        store.append(b"one") if store.size() < 1 else None
        root = store.root(1)
        note = algo_anchor.encode_note(ORIGIN, 1, root)
    if signature is None:
        signature = {"pqsig": _indexer_pqsig(sig=bytes(pq_auth))}
    payload = {
        "current-round": confirmed_round + 1,
        "transaction": {
            "id": txid,
            "confirmed-round": confirmed_round,
            "genesis-id": genesis,
            "sender": addr,
            "fee": fee,
            "tx-type": tx_type,
            "note": base64.b64encode(bytes(note)).decode("ascii"),
            "payment-transaction": {
                "amount": amount,
                "receiver": rcv,
                "close-remainder-to": close,
            },
            "rekey-to": rekey,
            "group": group,
            "lease": lease,
            "signature": signature,
            "sig-type": sig_type,
        },
    }
    if extra:
        payload["transaction"].update(extra)
    return payload


class TestNetDPlumbingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        worker.clear_queue()
        self.sender = payment.DEFAULT_PAYTO_ALGORAND
        self._env_keys = (
            "LIVE402_PQ_FALCON_NETWORK",
            "LIVE402_PQ_FALCON_BROADCAST",
            "LIVE402_PQ_FALCON_ADDRESS",
            "LIVE402_PQ_SIGNER_TOKEN",
            "LIVE402_PQ_SIGNER_HOST",
            "LIVE402_PQ_SIGNER_PORT",
        )
        for key in self._env_keys:
            os.environ.pop(key, None)
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = self.sender
        self._stop = False
        self._threads = []
        self._socks = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self._stop = True
        for sock in self._socks:
            try:
                sock.close()
            except Exception:
                pass
        for thread in self._threads:
            thread.join(timeout=2)
        worker.clear_queue()
        store.reset()
        for key in self._env_keys:
            os.environ.pop(key, None)
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        self.tmp.cleanup()

    def _serve(self, blobs, pq1=False):
        received = []

        def serve(sock):
            sock.listen(1)
            sock.settimeout(2)
            while not self._stop:
                try:
                    conn, _addr = sock.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                try:
                    raw = b""
                    while b"\n" not in raw:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        raw += chunk
                    req = json.loads(raw.split(b"\n", 1)[0].decode("utf-8"))
                    received.append(req)
                    if pq1:
                        blob = _pq1_signed(
                            req.get("tree_size"),
                            bytes.fromhex(req.get("root") or "00" * 32),
                            self.sender,
                        )
                    else:
                        blob = blobs[min(len(received) - 1, len(blobs) - 1)]
                    line = _reply(blob, tree_size=req.get("tree_size"), root=req.get("root"))
                    conn.sendall((line + "\n").encode("utf-8"))
                finally:
                    conn.close()

        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        self._socks.append(sock)
        thread = threading.Thread(target=serve, args=(sock,), daemon=True)
        thread.start()
        self._threads.append(thread)
        return port, received

    def _arm_sign(self, port):
        os.environ["LIVE402_PQ_SIGNER_TOKEN"] = _TOKEN
        os.environ["LIVE402_PQ_SIGNER_HOST"] = "127.0.0.1"
        os.environ["LIVE402_PQ_SIGNER_PORT"] = str(port)

    def _seed_authorized(self):
        store.append(b"one")
        store.save_checkpoint(1, _SIG_NOTE)
        signed = _pq1_signed(1, store.root(1), self.sender)
        port, received = self._serve([signed])
        self._arm_sign(port)
        out = worker.maybe_submit(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
        )
        self.assertEqual(out["signed"], signed)
        self.assertFalse(out["confirmed"])
        self._seed_signed = signed
        return received

    def test_broadcast_unset_never_posts(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        os.environ.pop("LIVE402_PQ_FALCON_BROADCAST", None)
        self._seed_authorized()
        posted = []

        def send_fn(blob):
            posted.append(blob)
            return _TXID

        out = worker.maybe_submit(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
            send_fn=send_fn,
        )
        self.assertFalse(out["submitted"])
        self.assertFalse(out["confirmed"])
        self.assertEqual(posted, [])
        self.assertEqual(worker.last_confirmed()["size"], 0)
        self.assertFalse(
            algo_anchor.submit_allowed(
                sender=self.sender,
                params={"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            )
        )

    def test_broadcast_posts_signedtxn_not_pqsig_marker(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        self._seed_authorized()
        posted = []

        def send_fn(blob):
            posted.append(blob)
            return _TXID

        out = worker.maybe_submit(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
            send_fn=send_fn,
        )
        self.assertTrue(out["submitted"])
        self.assertFalse(out["confirmed"])
        self.assertEqual(out["txid"], _TXID)
        self.assertEqual(posted, [self._seed_signed])
        self.assertNotEqual(posted[0], b"present")
        self.assertEqual(worker.last_confirmed()["size"], 0)
        self.assertIsNone(worker.public_anchor())
        auth = worker.last_authorized()
        self.assertTrue(auth["submitted"])
        self.assertEqual(auth["txid"], _TXID)
        again = worker.maybe_submit(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
            send_fn=send_fn,
        )
        self.assertTrue(again["submitted"])
        self.assertEqual(again["txid"], _TXID)
        self.assertEqual(posted, [self._seed_signed])
        self.assertEqual(worker.last_confirmed()["size"], 0)

    def test_fixture_without_mock_never_hits_live_algod(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        self._seed_authorized()
        with patch.object(algo_anchor, "_post_testnet", side_effect=AssertionError("live")):
            with patch.object(algo_anchor, "_get_pinned", side_effect=AssertionError("live")):
                out = worker.maybe_submit(
                    None,
                    self.sender,
                    {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
                    now=15 * 60,
                    send_fn=None,
                )
                self.assertFalse(out["submitted"])
                with self.assertRaises(algo_anchor.AnchorError):
                    worker.confirm_testnet_anchor(_TXID)

    def test_mainnet_genesis_has_no_submit_path(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        posted = []
        store.append(b"one")
        store.save_checkpoint(1, _SIG_NOTE)
        port, _received = self._serve([_SIGNED])
        self._arm_sign(port)
        out = worker.maybe_submit(
            None,
            self.sender,
            {"genesisID": algo_anchor.MAINNET_GENESIS_ID},
            now=15 * 60,
            send_fn=lambda blob: posted.append(blob) or _TXID,
        )
        self.assertIsNone(out)
        self.assertEqual(posted, [])
        self.assertFalse(
            algo_anchor.submit_allowed(
                sender=self.sender,
                params={"genesisID": algo_anchor.MAINNET_GENESIS_ID},
            )
        )
        self.assertIsNone(
            algo_anchor.send_if_allowed(
                _SIGNED,
                send_fn=lambda blob: posted.append(blob) or _TXID,
                params={"genesisID": algo_anchor.MAINNET_GENESIS_ID},
            )
        )
        self.assertEqual(posted, [])

    def test_token_unset_never_dials_even_with_broadcast(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        store.append(b"one")
        store.save_checkpoint(1, _SIG_NOTE)
        os.environ.pop("LIVE402_PQ_SIGNER_TOKEN", None)
        with patch("socket.create_connection", side_effect=AssertionError("must not dial")) as dial:
            out = worker.maybe_submit(
                None,
                self.sender,
                {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
                now=15 * 60,
                send_fn=lambda _b: _TXID,
            )
            dial.assert_not_called()
        self.assertIsNone(out)

    def test_signing_success_does_not_confirm(self):
        self._seed_authorized()
        self.assertEqual(worker.last_confirmed()["size"], 0)
        self.assertIsNone(worker.public_anchor())

    def test_confirm_rejects_placeholder_and_does_not_persist(self):
        self._seed_authorized()
        for txid in ("", "YOUR_TXID", "placeholder", "txid"):
            with self.assertRaises(algo_anchor.AnchorError):
                worker.confirm_testnet_anchor(txid, fetch_fn=lambda _t: _indexer_payload())
        self.assertEqual(worker.last_confirmed()["size"], 0)

    def test_confirm_ignores_caller_fields_and_requires_fetch(self):
        self._seed_authorized()
        with self.assertRaises(algo_anchor.AnchorError):
            worker.confirm_testnet_anchor(
                _TXID,
                tree_size=1,
                confirmed_round=99,
                root=store.root(1),
                origin=ORIGIN,
            )
        self.assertEqual(worker.last_confirmed()["size"], 0)

    def test_confirm_rejects_pqsig_marker_as_chain_object(self):
        self._seed_authorized()

        def fetch(_txid):
            return {
                "ok": True,
                "tree_size": 1,
                "root": "00" * 32,
                "pqsig": "present",
                "signed": _SIGNED.hex(),
            }

        with self.assertRaises(algo_anchor.AnchorError):
            worker.confirm_testnet_anchor(_TXID, fetch_fn=fetch)
        self.assertEqual(worker.last_confirmed()["size"], 0)

    def test_confirm_persists_only_after_independent_verify(self):
        self._seed_authorized()
        root = store.root(1)
        note = algo_anchor.encode_note(ORIGIN, 1, root)
        payload = _indexer_payload(note=note)

        def fetch(txid):
            self.assertEqual(txid, _TXID)
            return payload

        out = worker.confirm_testnet_anchor(_TXID, fetch_fn=fetch, at=50)
        self.assertEqual(out["size"], 1)
        self.assertEqual(out["txid"], _TXID)
        self.assertEqual(out["round"], 99)
        self.assertEqual(worker.last_confirmed()["size"], 1)
        self.assertEqual(worker.public_anchor()["txid"], _TXID)
        self.assertEqual(
            worker.public_anchor()["explorer"],
            algo_anchor.TESTNET_EXPLORER_TX_URL + _TXID,
        )
        self.assertFalse(worker.should_build(now=15 * 60, tree_size=1))

    def test_confirm_rejects_forgeries(self):
        self._seed_authorized()
        root = store.root(1)
        note = algo_anchor.encode_note(ORIGIN, 1, root)
        cases = [
            _indexer_payload(note=note, genesis=algo_anchor.MAINNET_GENESIS_ID),
            _indexer_payload(note=note, amount=1),
            _indexer_payload(note=note, fee=30001),
            _indexer_payload(note=note, close=self.sender),
            _indexer_payload(note=note, rekey=self.sender),
            _indexer_payload(note=note, group="abc"),
            _indexer_payload(note=note, lease="abc"),
            _indexer_payload(note=note, tx_type="axfer", extra={"asset-transfer-transaction": {"amount": 0}}),
            _indexer_payload(note=note, receiver=payment.DEFAULT_PAYTO_ALGORAND[:-1] + "A"),
            _indexer_payload(note=note, pq_auth=b"present"),
            _indexer_payload(note=b"\x00" * 84),
        ]
        for payload in cases:
            with self.assertRaises(algo_anchor.AnchorError):
                worker.confirm_testnet_anchor(_TXID, fetch_fn=lambda _t, p=payload: p)
            self.assertEqual(worker.last_confirmed()["size"], 0, payload["transaction"].get("tx-type"))

    def test_confirm_rejects_note_mismatch_vs_authorized(self):
        self._seed_authorized()
        other = algo_anchor.encode_note(ORIGIN, 1, b"\xff" * 32)
        with self.assertRaises(algo_anchor.AnchorError):
            worker.confirm_testnet_anchor(_TXID, fetch_fn=lambda _t: _indexer_payload(note=other))
        self.assertEqual(worker.last_confirmed()["size"], 0)

    def test_submit_allowed_true_only_when_gates_set(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        self.assertTrue(
            algo_anchor.submit_allowed(
                sender=self.sender,
                params={"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            )
        )
        os.environ.pop("LIVE402_PQ_FALCON_BROADCAST", None)
        self.assertFalse(
            algo_anchor.submit_allowed(
                sender=self.sender,
                params={"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            )
        )

    def test_official_indexer_pqsig_f1_confirms(self):
        self._seed_authorized()
        note = algo_anchor.encode_note(ORIGIN, 1, store.root(1))
        payload = _indexer_payload(note=note)
        self.assertEqual(payload["transaction"]["signature"]["pqsig"]["scheme"], "f1")
        out = worker.confirm_testnet_anchor(_TXID, fetch_fn=lambda _t: payload, at=50)
        self.assertEqual(out["txid"], _TXID)
        self.assertEqual(worker.last_confirmed()["txid"], _TXID)

    def test_official_codec_pqsig_f1_confirms(self):
        self._seed_authorized()
        note = algo_anchor.encode_note(ORIGIN, 1, store.root(1))
        addr = self.sender
        payload = {
            "id": _TXID,
            "confirmed-round": 99,
            "txn": {
                "type": "pay",
                "snd": addr,
                "rcv": addr,
                "amt": 0,
                "fee": 3000,
                "gen": algo_anchor.TESTNET_GENESIS_ID,
                "note": base64.b64encode(note).decode("ascii"),
            },
            "pqsig": _codec_pqsig(),
        }
        out = worker.confirm_testnet_anchor(_TXID, fetch_fn=lambda _t: payload, at=50)
        self.assertEqual(out["txid"], _TXID)
        self.assertEqual(worker.public_anchor()["txid"], _TXID)

    def test_pq_auth_rejects_non_official_envelopes(self):
        self._seed_authorized()
        note = algo_anchor.encode_note(ORIGIN, 1, store.root(1))
        cases = [
            _indexer_payload(
                note=note,
                signature={"falcon": base64.b64encode(_FALCON_AUTH).decode("ascii")},
            ),
            _indexer_payload(
                note=note,
                signature={"sig": base64.b64encode(b"\x11" * 64).decode("ascii")},
                sig_type="sig",
            ),
            _indexer_payload(note=note, signature={}),
            _indexer_payload(note=note, signature={"pqsig": _indexer_pqsig(scheme="")}),
            _indexer_payload(note=note, signature={"pqsig": _indexer_pqsig(scheme="f5")}),
            _indexer_payload(note=note, signature={"pqsig": _indexer_pqsig(pk=None, sig=_FALCON_AUTH)}),
            _indexer_payload(note=note, signature={"pqsig": _indexer_pqsig(pk=_FALCON_PK, sig=None)}),
            _indexer_payload(note=note, signature={"pqsig": _indexer_pqsig(pk=b"", sig=_FALCON_AUTH)}),
            _indexer_payload(note=note, signature={"pqsig": "present"}),
        ]
        for payload in cases:
            with self.assertRaises(algo_anchor.AnchorError):
                worker.confirm_testnet_anchor(_TXID, fetch_fn=lambda _t, p=payload: p)
            self.assertEqual(worker.last_confirmed()["size"], 0)

    def test_tick_confirms_only_after_independent_fetch(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        self._seed_authorized()
        posted = []

        def send_fn(blob):
            posted.append(blob)
            return _TXID

        note = algo_anchor.encode_note(ORIGIN, 1, store.root(1))
        fetched = []

        def fetch(txid):
            fetched.append(txid)
            return _indexer_payload(note=note)

        out = worker.tick(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
            send_fn=send_fn,
            fetch_fn=fetch,
        )
        self.assertEqual(posted, [self._seed_signed])
        self.assertEqual(fetched, [_TXID])
        self.assertEqual(out["txid"], _TXID)
        self.assertEqual(worker.last_confirmed()["txid"], _TXID)
        worker.tick(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
            send_fn=send_fn,
            fetch_fn=fetch,
        )
        self.assertEqual(posted, [self._seed_signed])
        self.assertEqual(fetched, [_TXID])

    def test_tick_does_not_confirm_from_post_alone(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        self._seed_authorized()
        out = worker.tick(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
            send_fn=lambda _b: _TXID,
            fetch_fn=lambda _t: None,
        )
        self.assertIsNone(out)
        self.assertTrue(worker.last_authorized()["submitted"])
        self.assertEqual(worker.last_authorized()["txid"], _TXID)
        self.assertEqual(worker.last_confirmed()["size"], 0)
        self.assertIsNone(worker.public_anchor())

    def test_production_loop_calls_existing_tick(self):
        src = inspect.getsource(catalog._trickle_loop)
        self.assertNotIn("pq_worker.tick", src)
        self.assertIn("confirm_testnet_anchor", inspect.getsource(worker.tick))
        main_src = inspect.getsource(__import__("live402.server", fromlist=["main"]).main)
        self.assertIn("start_refresher", main_src)
        self.assertIn("start_worker", main_src)

    def test_single_unconfirmed_anchor_invariant(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        store.append(b"one")
        store.save_checkpoint(1, _signed_note(1, store.root(1)))
        signed_n = _pq1_signed(1, store.root(1), self.sender)
        port, received = self._serve([signed_n], pq1=True)
        self._arm_sign(port)
        posted = []

        def send_fn(blob):
            posted.append(blob)
            return _TXID

        out = worker.maybe_submit(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
            send_fn=send_fn,
        )
        self.assertEqual(out["tree_size"], 1)
        self.assertTrue(out["submitted"])
        self.assertEqual(out["txid"], _TXID)
        self.assertEqual(len(received), 1)
        self.assertEqual(posted, [signed_n])
        self.assertEqual(worker.last_authorized()["size"], 1)

        store.append(b"two")
        self.assertEqual(store.size(), 2)
        store.save_checkpoint(2, _signed_note(2, store.root(2)))
        self.assertFalse(worker.should_build(now=30 * 60, tree_size=2))

        for _ in range(5):
            again = worker.tick(
                None,
                self.sender,
                {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
                now=30 * 60,
                send_fn=send_fn,
                fetch_fn=lambda _t: None,
            )
            self.assertIsNone(again)

        self.assertEqual(len(received), 1)
        self.assertEqual(posted, [signed_n])
        self.assertEqual(worker.last_authorized()["size"], 1)
        self.assertEqual(worker.last_authorized()["txid"], _TXID)
        self.assertEqual(worker.last_confirmed()["size"], 0)

        note1 = algo_anchor.encode_note(ORIGIN, 1, store.root(1))
        confirmed = worker.confirm_testnet_anchor(
            _TXID,
            fetch_fn=lambda _t: _indexer_payload(note=note1),
            at=50,
        )
        self.assertEqual(confirmed["size"], 1)
        self.assertEqual(worker.last_confirmed()["size"], 1)

        later = worker.tick(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=50 + 15 * 60,
            send_fn=lambda blob: posted.append(blob) or ("C" * 52),
            fetch_fn=lambda _t: None,
        )
        self.assertIsNone(later)
        self.assertEqual(len(received), 2)
        self.assertEqual(received[1]["tree_size"], 2)
        self.assertEqual(worker.last_authorized()["size"], 2)
        self.assertEqual(posted[-1], _pq1_signed(2, store.root(2), self.sender))
        self.assertEqual(worker.last_confirmed()["size"], 1)

    def test_existing_authorized_row_recovered_when_meta_empty(self):
        store.append(b"one")
        store.save_authorized_checkpoint(
            tree_size=1,
            origin=ORIGIN,
            root=store.root(1),
            checkpoint=_SIG_NOTE,
            request_id="rid-live",
            signed=_SIGNED,
            at=7,
            submitted=True,
            txid=_TXID,
        )
        store.meta_set("last_authorized_checkpoint", "")
        store.close()
        auth = worker.last_authorized()
        self.assertEqual(auth["size"], 1)
        self.assertEqual(auth["signed"], _SIGNED)
        self.assertTrue(auth["submitted"])
        self.assertEqual(auth["txid"], _TXID)
        self.assertEqual(auth["request_id"], "rid-live")

    def test_confirm_rejects_nonempty_sgnr(self):
        self._seed_authorized()
        note = algo_anchor.encode_note(ORIGIN, 1, store.root(1))
        payload = {
            "id": _TXID,
            "confirmed-round": 99,
            "txn": {
                "type": "pay",
                "snd": self.sender,
                "rcv": self.sender,
                "amt": 0,
                "fee": 3000,
                "gen": algo_anchor.TESTNET_GENESIS_ID,
                "note": base64.b64encode(note).decode("ascii"),
            },
            "pqsig": _codec_pqsig(),
            "sgnr": self.sender,
        }
        with self.assertRaises(algo_anchor.AnchorError):
            worker.confirm_testnet_anchor(_TXID, fetch_fn=lambda _t: payload)
        self.assertEqual(worker.last_confirmed()["size"], 0)

    def test_confirm_rejects_nonempty_auth_addr(self):
        self._seed_authorized()
        note = algo_anchor.encode_note(ORIGIN, 1, store.root(1))
        payload = _indexer_payload(note=note)
        payload["transaction"]["auth-addr"] = self.sender
        with self.assertRaises(algo_anchor.AnchorError):
            worker.confirm_testnet_anchor(_TXID, fetch_fn=lambda _t: payload)
        self.assertEqual(worker.last_confirmed()["size"], 0)

    def test_confirm_rejects_nonempty_authAddr(self):
        self._seed_authorized()
        note = algo_anchor.encode_note(ORIGIN, 1, store.root(1))
        payload = _indexer_payload(note=note)
        payload["transaction"]["authAddr"] = self.sender
        with self.assertRaises(algo_anchor.AnchorError):
            worker.confirm_testnet_anchor(_TXID, fetch_fn=lambda _t: payload)
        self.assertEqual(worker.last_confirmed()["size"], 0)

    def test_broadcast_copy_is_router_env(self):
        readme = Path(__file__).resolve().parent.parent.joinpath("README.md").read_text(encoding="utf-8")
        self.assertNotIn("Not set on 402signal", readme)
        self.assertIn("402signal (router) env", readme)
        self.assertIn("402security must GO before anyone sets it to `1`", readme)
        self.assertIn("Signer never reads BROADCAST", readme)
        self.assertNotIn("lives on the signer", readme.lower())
        comments = (
            inspect.getsource(algo_anchor)
            + inspect.getsource(worker)
            + Path(__file__).resolve().parent.parent.joinpath("live402", "discover.py").read_text(encoding="utf-8")
        )
        self.assertNotIn("Not set on 402signal", comments)
        self.assertNotIn("lives on the signer", comments.lower())


class HomepagePqSectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = payment.DEFAULT_PAYTO_ALGORAND
        store.reset()
        worker.clear_queue()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        worker.clear_queue()
        store.reset()
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        os.environ.pop("LIVE402_PQ_FALCON_ADDRESS", None)
        self.tmp.cleanup()

    def _get(self, path="/"):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        res = conn.getresponse()
        raw = res.read().decode("utf-8")
        conn.close()
        return res.status, raw

    def test_homepage_omits_pq_without_last_confirmed(self):
        status, html = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("id=\"pq-log\"", html)
        self.assertIn("Verifiable routing history", html)
        self.assertIn("Awaiting anchor", html)
        self.assertIn("Algorand MainNet", html)
        self.assertNotIn("Latest confirmed Tree", html)
        self.assertNotIn("View TestNet transaction", html)
        self.assertNotIn("testnet.explorer.perawallet.app", html)
        self.assertIn("Find a paid API that works right now.", html)
        self.assertNotIn("why it won", html.lower())
        self.assertNotIn("healthy", html)
        self.assertNotIn("Executable Now Rate", html)

    def test_homepage_renders_pq_only_after_confirmed(self):
        store.append(b"one")
        store.save_authorized_checkpoint(
            tree_size=1,
            origin=ORIGIN,
            root=store.root(1),
            checkpoint=_SIG_NOTE,
            request_id="rid",
            signed=_SIGNED,
            at=1,
        )
        note = algo_anchor.encode_note(ORIGIN, 1, store.root(1))
        worker.confirm_testnet_anchor(_TXID, fetch_fn=lambda _t: _indexer_payload(note=note), at=2)
        status, html = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("id=\"pq-log\"", html)
        self.assertIn("Algorand MainNet", html)
        self.assertIn("Verifiable routing history", html)
        self.assertIn("append-only log", html)
        self.assertIn("Awaiting anchor", html)
        self.assertNotIn('class="pq-chip is-anchored"', html)
        self.assertNotIn("Latest confirmed Tree", html)
        self.assertIn("View transparency", html)
        self.assertNotIn("Latest checkpoint · Tree", html)
        self.assertIn('href="/transparency"', html)
        self.assertNotIn("View TestNet transaction", html)
        self.assertNotIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TXID, html)
        self.assertNotIn("why it won", html.lower())
        self.assertNotIn("healthy", html)
        self.assertNotIn("Executable Now Rate", html)
        self.assertNotIn("success_7d", html)
        self.assertIn("Algorand MainNet", html)
        self.assertNotIn("placeholder", html.lower())
        self.assertIn("Find a paid API that works right now.", html)
        self.assertEqual(html.count("<h1"), 1)


if __name__ == "__main__":
    unittest.main()
