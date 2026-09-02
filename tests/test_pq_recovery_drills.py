"""Recovery drills A-F as fixture tests. No MainNet network."""

from __future__ import annotations

import base64
import importlib.util
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import payment
from live402.pq import ORIGIN, ORIGIN_MAINNET, algo_anchor, store, worker
from live402.pq import checkpoint as ckpt
from live402.pq import ops_state
from tests.pq_test_env import clear_pq_env, falcon_f1_fixture_pk, falcon_f1_fixture_sig


_TOKEN = "vector-token"
_TXID = "B" * 52
_SIGNED_A = b"STXN-authorized-A" + bytes(range(24))
_FALCON_AUTH = falcon_f1_fixture_sig(b"FALCON-PQ-AUTH")
_FALCON_PK = falcon_f1_fixture_pk(b"FALCON-PK")
_SIG = base64.b64encode(b"\x00" * 4 + b"\x22" * 64).decode("ascii")


def _note(size, root, origin=ORIGIN):
    body = ckpt.checkpoint_body(origin, int(size), bytes(root))
    return "%s\n%s %s %s\n" % (body, ckpt.EMDASH, origin, _SIG)


def _load_script(name):
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / ("%s.py" % name)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _indexer_payload(note, *, txid=_TXID, genesis=algo_anchor.TESTNET_GENESIS_ID):
    addr = payment.DEFAULT_PAYTO_ALGORAND
    return {
        "transaction": {
            "id": txid,
            "confirmed-round": 99,
            "genesis-id": genesis,
            "tx-type": "pay",
            "sender": addr,
            "fee": 3000,
            "note": base64.b64encode(bytes(note)).decode("ascii"),
            "payment-transaction": {"amount": 0, "receiver": addr},
            "signature": {
                "pqsig": {
                    "scheme": "f1",
                    "salt": 0,
                    "public-key": base64.b64encode(_FALCON_PK).decode("ascii"),
                    "signature": base64.b64encode(_FALCON_AUTH).decode("ascii"),
                }
            },
        }
    }


class RecoveryDrillsAFTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clear_pq_env()
        ops_state.reset()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        worker.clear_queue()
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
        clear_pq_env()
        ops_state.reset()
        store.reset()
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
                    import json

                    req = json.loads(raw.split(b"\n", 1)[0].decode("utf-8"))
                    received.append(req)
                    blob = blobs[min(len(received) - 1, len(blobs) - 1)]
                    line = json.dumps(
                        {
                            "ok": True,
                            "tree_size": int(req.get("tree_size") or 1),
                            "root": req.get("root") or "00" * 32,
                            "pqsig": "present",
                            "signed": blob.hex(),
                        },
                        separators=(",", ":"),
                    )
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

    def test_a_backup_restore_identity(self):
        store.append(b"drill-a-one")
        store.append(b"drill-a-two")
        size = store.size()
        root = store.root(size)
        note = _note(size, root)
        store.save_checkpoint(size, note)
        src = Path(store.db_path())
        dest_dir = Path(self.tmp.name) / "snap"
        dest_dir.mkdir()
        drill = _load_script("pq_log_restore_drill")
        os.environ["LIVE402_HISTORY_DB"] = str(Path(self.tmp.name) / "missing-history.sqlite")
        os.environ["LIVE402_CATALOG_DB"] = str(Path(self.tmp.name) / "missing-catalog.sqlite")
        result = drill.run_drill(src, dest_dir)
        self.assertTrue(result["ok"])
        self.assertEqual(result["size"], size)
        self.assertEqual(result["root"], root.hex())
        self.assertTrue(src.is_file())
        self.assertNotIn("/data/", result["restored"])
        with self.assertRaises(SystemExit):
            drill.run_drill(src, Path("/data/forbidden-drill"))

    def test_b_authorized_not_submitted(self):
        store.append(b"one")
        root = store.root(1)
        note = _note(1, root)
        store.save_checkpoint(1, note)
        port, received = self._serve([_SIGNED_A])
        self._arm_sign(port)
        out = worker.maybe_submit(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
        )
        self.assertEqual(out["signed"], _SIGNED_A)
        self.assertFalse(out["submitted"])
        self.assertFalse(out["confirmed"])
        with patch("socket.create_connection", side_effect=AssertionError("must not re-dial")) as dial:
            again = worker.maybe_submit(
                None,
                self.sender,
                {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
                now=15 * 60,
            )
            dial.assert_not_called()
        self.assertEqual(again["signed"], _SIGNED_A)
        self.assertEqual(len(received), 1)
        self.assertEqual(worker.last_confirmed()["size"], 0)
        self.assertFalse(worker.last_authorized()["submitted"])

    def test_c_submitted_not_confirmed(self):
        store.append(b"one")
        root = store.root(1)
        note = _note(1, root)
        store.save_checkpoint(1, note)
        store.save_authorized_checkpoint(
            tree_size=1,
            origin=ORIGIN,
            root=root,
            checkpoint=note,
            request_id="rid-c",
            signed=_SIGNED_A,
            at=10,
            submitted=True,
            txid=_TXID,
        )
        sent = []
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = self.sender
        os.environ["LIVE402_PQ_SIGNER_TOKEN"] = _TOKEN
        out = worker.maybe_submit(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
            send_fn=lambda b: sent.append(b) or "C" * 52,
        )
        self.assertEqual(sent, [])
        self.assertTrue(out["submitted"])
        self.assertEqual(out["txid"], _TXID)
        self.assertEqual(worker.last_confirmed()["size"], 0)
        chain_note = algo_anchor.encode_note(ORIGIN, 1, root)
        confirmed = worker.maybe_confirm(
            fetch_fn=lambda _t: _indexer_payload(chain_note),
            at=50,
        )
        self.assertEqual(confirmed["size"], 1)
        self.assertEqual(confirmed["txid"], _TXID)
        self.assertEqual(worker.last_confirmed()["size"], 1)

    def test_d_authorized_mismatch_fail_closed(self):
        store.append(b"one")
        root = store.root(1)
        note = _note(1, root)
        store.save_checkpoint(1, note)
        port, received = self._serve([_SIGNED_A, b"OTHER"])
        self._arm_sign(port)
        worker.maybe_submit(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
        )
        other_root = "ff" * 32
        with self.assertRaises(worker.AuthorizedConflict):
            worker._recover_authorized(1, ORIGIN, other_root, note)
        with patch.object(store, "root", return_value=bytes.fromhex(other_root)):
            with patch("socket.create_connection", side_effect=AssertionError("must not re-dial")) as dial:
                out = worker.maybe_submit(
                    None,
                    self.sender,
                    {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
                    now=15 * 60,
                )
                dial.assert_not_called()
        self.assertIsNone(out)
        self.assertEqual(store.authorized_at(1)["signed"], _SIGNED_A)
        self.assertEqual(len(received), 1)
        self.assertEqual(ops_state.snapshot()["recovery_conflicts"], 1)
        self.assertEqual(worker.last_confirmed()["size"], 0)

    def test_e_fresh_mainnet_identity(self):
        for i in range(3):
            store.append(("tn-%d" % i).encode("ascii"))
        testnet_size = store.size()
        testnet_root = store.root(testnet_size)
        testnet_db = store.db_path()
        store.close()
        mainnet_db = os.path.join(self.tmp.name, "pq-log-mainnet.sqlite")
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_LOG_DB"] = mainnet_db
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN_MAINNET
        store.close()
        self.assertEqual(store.size(), 0)
        self.assertEqual(store.origin(), ORIGIN_MAINNET)
        rec = store.append(b"first-mainnet")
        self.assertEqual(rec["size"], 1)
        store.close()
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "testnet-v1"
        os.environ["LIVE402_PQ_LOG_DB"] = testnet_db
        os.environ.pop("LIVE402_PQ_LOG_ORIGIN", None)
        store.close()
        self.assertEqual(store.size(), testnet_size)
        self.assertEqual(store.root(testnet_size), testnet_root)
        self.assertEqual(store.origin(), ORIGIN)
        self.assertNotEqual(store.origin(), ORIGIN_MAINNET)

    def test_f_misconfig_and_kill_switch_never_sends(self):
        sent = []
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ.pop("LIVE402_PQ_FALCON_MAINNET_BROADCAST", None)
        self.assertIsNone(
            algo_anchor.send_if_allowed(
                b"STXN",
                send_fn=lambda b: sent.append(b) or _TXID,
                params={"genesisID": algo_anchor.MAINNET_GENESIS_ID},
            )
        )
        os.environ["LIVE402_PQ_FALCON_MAINNET_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        self.assertIsNone(
            algo_anchor.send_if_allowed(
                b"STXN",
                send_fn=lambda b: sent.append(b) or _TXID,
                params={"genesisID": algo_anchor.MAINNET_GENESIS_ID},
            )
        )
        self.assertEqual(sent, [])
        body = payment.payment_required("https://402signal.com/route")
        self.assertTrue(body.get("accepts"))


if __name__ == "__main__":
    unittest.main()
