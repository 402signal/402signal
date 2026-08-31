"""Authorized vs confirmed PQ anchor state. C1: TOKEN unset never dials."""

from __future__ import annotations

import base64
import json
import os
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import payment
from live402.pq import ORIGIN, algo_anchor, signer_client, store, worker
from live402.pq import checkpoint as ckpt


_TOKEN = "vector-token"
_ORIGIN = "402signal.com/pq/log"
_SIG = base64.b64encode(b"\x00" * 4 + b"\x22" * 64).decode("ascii")
_SIG_B = base64.b64encode(b"\x00" * 4 + b"\x33" * 64).decode("ascii")
_BODY = "402signal.com/pq/log\n1\nAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n"
_NOTE_A = "%s\n%s %s %s\n" % (_BODY, ckpt.EMDASH, _ORIGIN, _SIG)
_NOTE_B = "%s\n%s %s %s\n" % (_BODY, ckpt.EMDASH, _ORIGIN, _SIG_B)
_SIGNED_A = b"STXN-authorized-A" + bytes(range(24))
_SIGNED_B = b"STXN-authorized-B" + bytes(range(24))
_TXID = "B" * 52


def _reply(signed):
    return json.dumps(
        {
            "ok": True,
            "tree_size": 1,
            "root": "00" * 32,
            "pqsig": "present",
            "signed": signed.hex(),
        },
        separators=(",", ":"),
    )


class AuthorizedConfirmedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        worker.clear_queue()
        os.environ.pop("LIVE402_PQ_SIGNER_TOKEN", None)
        os.environ.pop("LIVE402_PQ_SIGNER_HOST", None)
        os.environ.pop("LIVE402_PQ_SIGNER_PORT", None)
        os.environ.pop("LIVE402_PQ_FALCON_BROADCAST", None)
        self.sender = payment.DEFAULT_PAYTO_ALGORAND
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
        os.environ.pop("LIVE402_PQ_SIGNER_TOKEN", None)
        os.environ.pop("LIVE402_PQ_SIGNER_HOST", None)
        os.environ.pop("LIVE402_PQ_SIGNER_PORT", None)
        os.environ.pop("LIVE402_PQ_FALCON_BROADCAST", None)
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        self.tmp.cleanup()

    def _serve(self, blobs):
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
                    received.append(json.loads(raw.split(b"\n", 1)[0].decode("utf-8")))
                    blob = blobs[min(len(received) - 1, len(blobs) - 1)]
                    conn.sendall((_reply(blob) + "\n").encode("utf-8"))
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

    def _seed_note(self, note=_NOTE_A):
        store.append(b"one")
        store.save_checkpoint(1, note)
        ckpt.parse_signed_note(note)

    def test_token_unset_never_dials(self):
        self._seed_note()
        os.environ.pop("LIVE402_PQ_SIGNER_TOKEN", None)
        with patch("socket.create_connection", side_effect=AssertionError("must not dial")) as dial:
            out = worker.maybe_submit(
                None,
                self.sender,
                {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
                now=15 * 60,
            )
            dial.assert_not_called()
        self.assertIsNone(out)
        self.assertEqual(worker.last_authorized()["size"], 0)
        self.assertEqual(worker.last_confirmed()["size"], 0)

    def test_signed_updates_authorized_not_confirmed(self):
        self._seed_note()
        port, received = self._serve([_SIGNED_A])
        self._arm_sign(port)
        out = worker.maybe_submit(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
        )
        self.assertEqual(out["signed"], _SIGNED_A)
        self.assertTrue(out["authorized"])
        self.assertFalse(out["confirmed"])
        self.assertFalse(out["submitted"])
        self.assertEqual(worker.last_authorized()["size"], 1)
        self.assertEqual(worker.last_authorized()["request_id"], received[0]["request_id"])
        self.assertEqual(worker.last_authorized()["signed"], _SIGNED_A)
        self.assertEqual(worker.last_authorized()["checkpoint"], _NOTE_A)
        self.assertEqual(worker.last_confirmed()["size"], 0)
        self.assertEqual(worker.last_confirmed()["txid"], "")
        self.assertEqual(worker.last_anchor(), {"size": 0, "at": 0})
        self.assertIsNone(worker.public_anchor())

    def test_should_build_does_not_mistake_authorized_for_confirmed(self):
        self._seed_note()
        port, _received = self._serve([_SIGNED_A])
        self._arm_sign(port)
        worker.maybe_submit(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
        )
        self.assertEqual(worker.last_authorized()["size"], 1)
        self.assertEqual(worker.last_confirmed()["size"], 0)
        self.assertTrue(worker.should_build(now=15 * 60, tree_size=1))
        self.assertIsNone(worker.public_anchor())

    def test_signed_unbroadcast_is_retryable_and_idempotent(self):
        self._seed_note()
        port, received = self._serve([_SIGNED_A, _SIGNED_B])
        self._arm_sign(port)
        first = worker.maybe_submit(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
        )
        rid = first["request_id"]
        self.assertTrue(worker.should_build(now=15 * 60, tree_size=1))
        with patch("socket.create_connection", side_effect=AssertionError("must not re-dial")) as dial:
            second = worker.maybe_submit(
                None,
                self.sender,
                {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
                now=15 * 60,
            )
            dial.assert_not_called()
        self.assertEqual(second["signed"], _SIGNED_A)
        self.assertEqual(second["request_id"], rid)
        self.assertEqual(len(received), 1)
        self.assertEqual(worker.last_authorized()["signed"], _SIGNED_A)
        self.assertEqual(worker.last_confirmed()["size"], 0)

    def test_retry_does_not_authorize_a_different_checkpoint(self):
        self._seed_note(_NOTE_A)
        port, received = self._serve([_SIGNED_A, _SIGNED_B])
        self._arm_sign(port)
        worker.maybe_submit(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
        )
        store.save_checkpoint(1, _NOTE_B)
        with patch("socket.create_connection", side_effect=AssertionError("must not re-dial")) as dial:
            out = worker.maybe_submit(
                None,
                self.sender,
                {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
                now=15 * 60,
            )
            dial.assert_not_called()
        self.assertEqual(out["signed"], _SIGNED_A)
        self.assertNotEqual(out["signed"], _SIGNED_B)
        self.assertEqual(store.authorized_at(1)["checkpoint"], _NOTE_A)
        self.assertEqual(len(received), 1)

    def test_restart_preserves_authorized_not_confirmed(self):
        self._seed_note()
        port, _received = self._serve([_SIGNED_A])
        self._arm_sign(port)
        worker.maybe_submit(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
        )
        store.close()
        auth = worker.last_authorized()
        conf = worker.last_confirmed()
        self.assertEqual(auth["size"], 1)
        self.assertEqual(auth["signed"], _SIGNED_A)
        self.assertTrue(auth["request_id"])
        self.assertEqual(conf["size"], 0)
        self.assertEqual(worker.last_anchor()["size"], 0)

    def test_confirmed_only_after_explicit_path(self):
        self._seed_note()
        port, _received = self._serve([_SIGNED_A])
        self._arm_sign(port)
        worker.maybe_submit(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
        )
        self.assertEqual(worker.last_confirmed()["size"], 0)
        with self.assertRaises(algo_anchor.AnchorError):
            worker.confirm_testnet_anchor(
                tree_size=1,
                txid="YOUR_TXID",
                confirmed_round=10,
                root=store.root(1),
            )
        with self.assertRaises(algo_anchor.AnchorError):
            worker.confirm_testnet_anchor(
                tree_size=1,
                txid="placeholder",
                confirmed_round=10,
                root=store.root(1),
            )
        out = worker.confirm_testnet_anchor(
            tree_size=1,
            txid=_TXID,
            confirmed_round=99,
            root=store.root(1),
            at=50,
        )
        self.assertEqual(out["size"], 1)
        self.assertEqual(out["txid"], _TXID)
        self.assertEqual(out["round"], 99)
        self.assertEqual(worker.last_confirmed()["size"], 1)
        self.assertEqual(worker.last_anchor()["size"], 1)
        self.assertEqual(worker.public_anchor()["txid"], _TXID)
        self.assertFalse(worker.should_build(now=15 * 60, tree_size=1))
        self.assertNotIn("placeholder", worker.public_anchor()["txid"].lower())

    def test_legacy_anchor_meta_migrates_to_authorized_not_confirmed(self):
        store.meta_set("anchor", json.dumps({"size": 3, "at": 9}))
        store.close()
        auth = worker.last_authorized()
        self.assertEqual(auth["size"], 3)
        self.assertEqual(auth["at"], 9)
        self.assertEqual(worker.last_confirmed()["size"], 0)
        self.assertEqual(worker.last_anchor()["size"], 0)
        self.assertIsNone(worker.public_anchor())

    def test_no_algod_post_on_confirm_or_sign(self):
        import inspect

        src = inspect.getsource(worker) + inspect.getsource(algo_anchor)
        self.assertNotIn("def _post_testnet", src)
        self.assertNotIn("testnet-api.algonode.cloud/v2/transactions", src)
        self.assertIn("Does not POST to algod", inspect.getsource(worker))


if __name__ == "__main__":
    unittest.main()
