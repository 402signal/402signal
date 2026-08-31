"""6PN pq-anchor/1 client. Token unset never dials. No Falcon SK. No submit."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import payment, server
from live402.pq import ORIGIN, algo_anchor, isolated_signer, signer_client, store, worker
from live402.pq import checkpoint as ckpt


# Independent MAC vector (pq-anchor/1 encoding). Do not derive from build_request.
_VECTOR_TOKEN = "vector-token"
_VECTOR_ORIGIN = "402signal.com/pq/log"
_VECTOR_ROOT = "00" * 32
_VECTOR_CHECKPOINT = (
    "402signal.com/pq/log\n1\nAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n"
)
_VECTOR_CANONICAL = (
    "pq-anchor/1\n"
    "checkpoint=" + _VECTOR_CHECKPOINT + "\n"
    "consistency=\n"
    "origin=402signal.com/pq/log\n"
    "request_id=req-vector-1\n"
    "root=" + _VECTOR_ROOT + "\n"
    "timestamp=1700000000\n"
    "tree_size=1\n"
    "v=1\n"
)
_VECTOR_MAC = hmac.new(
    _VECTOR_TOKEN.encode("utf-8"),
    _VECTOR_CANONICAL.encode("utf-8"),
    hashlib.sha256,
).hexdigest()

_VECTOR_CONS = ["aa" * 32, "bb" * 32]
_VECTOR_CONS_CANONICAL = (
    "pq-anchor/1\n"
    "checkpoint=" + _VECTOR_CHECKPOINT + "\n"
    "consistency=" + ",".join(_VECTOR_CONS) + "\n"
    "origin=402signal.com/pq/log\n"
    "request_id=req-vector-2\n"
    "root=" + _VECTOR_ROOT + "\n"
    "timestamp=1700000000\n"
    "tree_size=2\n"
    "v=1\n"
)
_VECTOR_CONS_MAC = hmac.new(
    _VECTOR_TOKEN.encode("utf-8"),
    _VECTOR_CONS_CANONICAL.encode("utf-8"),
    hashlib.sha256,
).hexdigest()


class SignerClientProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        worker.clear_queue()
        os.environ.pop("LIVE402_PQ_SIGNER_TOKEN", None)
        os.environ.pop("LIVE402_PQ_SIGNER_HOST", None)
        os.environ.pop("LIVE402_PQ_SIGNER_PORT", None)
        os.environ.pop("LIVE402_PQ_FALCON_NETWORK", None)
        os.environ.pop("LIVE402_PQ_FALCON_BROADCAST", None)
        os.environ.pop("LIVE402_PQ_FALCON_ADDRESS", None)
        os.environ.pop("LIVE402_PQ_FALCON_SK", None)
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
        os.environ.pop("LIVE402_PQ_FALCON_NETWORK", None)
        os.environ.pop("LIVE402_PQ_FALCON_BROADCAST", None)
        os.environ.pop("LIVE402_PQ_FALCON_ADDRESS", None)
        os.environ.pop("LIVE402_PQ_FALCON_SK", None)
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        self.tmp.cleanup()

    def test_canonical_mac_vector_empty_consistency(self):
        body = signer_client.canonical_bytes(
            origin=_VECTOR_ORIGIN,
            tree_size=1,
            root=_VECTOR_ROOT,
            consistency=[],
            timestamp=1700000000,
            request_id="req-vector-1",
            checkpoint=_VECTOR_CHECKPOINT,
        )
        self.assertEqual(body, _VECTOR_CANONICAL.encode("utf-8"))
        self.assertTrue(body.startswith(b"pq-anchor/1\n"))
        self.assertEqual(signer_client.mac_hex(_VECTOR_TOKEN, body), _VECTOR_MAC)
        payload = signer_client.build_request(
            origin=_VECTOR_ORIGIN,
            tree_size=1,
            root=bytes.fromhex(_VECTOR_ROOT),
            consistency=[],
            timestamp=1700000000,
            request_id="req-vector-1",
            checkpoint=_VECTOR_CHECKPOINT,
            token=_VECTOR_TOKEN,
        )
        self.assertEqual(payload["hmac"], _VECTOR_MAC)

    def test_canonical_mac_vector_consistency_csv(self):
        body = signer_client.canonical_bytes(
            origin=_VECTOR_ORIGIN,
            tree_size=2,
            root=_VECTOR_ROOT,
            consistency=_VECTOR_CONS,
            timestamp=1700000000,
            request_id="req-vector-2",
            checkpoint=_VECTOR_CHECKPOINT,
        )
        self.assertEqual(body, _VECTOR_CONS_CANONICAL.encode("utf-8"))
        self.assertEqual(signer_client.mac_hex(_VECTOR_TOKEN, body), _VECTOR_CONS_MAC)

    def test_token_unset_never_dials(self):
        os.environ.pop("LIVE402_PQ_SIGNER_TOKEN", None)
        self.assertFalse(signer_client.token_configured())
        with patch("socket.create_connection", side_effect=AssertionError("must not dial")) as dial:
            with self.assertRaises(signer_client.SignerClientError):
                signer_client.request_sign(
                    origin=ORIGIN,
                    tree_size=1,
                    root=b"\x00" * 32,
                    consistency=[],
                    checkpoint=_VECTOR_CHECKPOINT,
                    host="127.0.0.1",
                    port=1,
                )
            dial.assert_not_called()
        store.append(b"one")
        worker.save_anchor(0, 0)
        with patch("socket.create_connection", side_effect=AssertionError("must not dial")) as dial:
            out = worker.maybe_submit(None, self.sender, now=15 * 60, send_fn=lambda _b: "nope")
            dial.assert_not_called()
        self.assertIsNone(out)

    def test_empty_token_never_dials(self):
        os.environ["LIVE402_PQ_SIGNER_TOKEN"] = "   "
        with patch("socket.create_connection", side_effect=AssertionError("must not dial")) as dial:
            with self.assertRaises(signer_client.SignerClientError):
                signer_client.request_sign(
                    origin=ORIGIN,
                    tree_size=1,
                    root=b"\x00" * 32,
                    consistency=[],
                    checkpoint=_VECTOR_CHECKPOINT,
                )
            dial.assert_not_called()

    def test_request_sends_exactly_protocol_keys(self):
        payload = signer_client.build_request(
            origin=_VECTOR_ORIGIN,
            tree_size=1,
            root=_VECTOR_ROOT,
            consistency=["11" * 32],
            timestamp=1700000000,
            request_id="rid",
            checkpoint=_VECTOR_CHECKPOINT,
            token=_VECTOR_TOKEN,
        )
        self.assertEqual(list(payload), list(signer_client.REQUEST_KEYS))
        line = signer_client.encode_request_line(payload)
        data = json.loads(line)
        self.assertEqual(set(data), set(signer_client.REQUEST_KEYS))
        for key in (
            "fee",
            "firstValid",
            "firstRound",
            "sender",
            "snd",
            "amount",
            "amt",
            "txn",
            "unsigned",
            "pk",
            "sk",
        ):
            self.assertNotIn(key, data)
        extra = dict(payload)
        extra["fee"] = 3000
        with self.assertRaises(signer_client.SignerClientError):
            signer_client.encode_request_line(extra)

    def test_default_dial_is_6pn_9091_not_8080(self):
        os.environ.pop("LIVE402_PQ_SIGNER_HOST", None)
        os.environ.pop("LIVE402_PQ_SIGNER_PORT", None)
        self.assertEqual(signer_client.ipc_peer_host(), "402signal-pq-signer.internal")
        self.assertEqual(signer_client.ipc_port(), 9091)
        os.environ["LIVE402_PQ_SIGNER_PORT"] = "8080"
        with self.assertRaises(signer_client.SignerClientError):
            signer_client.ipc_port()
        os.environ.pop("LIVE402_PQ_SIGNER_PORT", None)

    def test_loopback_json_line_round_trip(self):
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
                    line = raw.split(b"\n", 1)[0].decode("utf-8")
                    data = json.loads(line)
                    received.append(data)
                    conn.sendall(json.dumps({"pqsig": b"pqsig-6pn".hex()}).encode("utf-8") + b"\n")
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
        os.environ["LIVE402_PQ_SIGNER_TOKEN"] = _VECTOR_TOKEN
        out = signer_client.request_sign(
            origin=_VECTOR_ORIGIN,
            tree_size=1,
            root=_VECTOR_ROOT,
            consistency=[],
            checkpoint=_VECTOR_CHECKPOINT,
            now=1700000000,
            request_id="req-vector-1",
            host="127.0.0.1",
            port=port,
        )
        self.assertEqual(out, b"pqsig-6pn")
        self.assertEqual(len(received), 1)
        self.assertEqual(set(received[0]), set(signer_client.REQUEST_KEYS))
        self.assertEqual(received[0]["hmac"], _VECTOR_MAC)
        self.assertEqual(received[0]["v"], 1)
        self.assertNotIn("txn", received[0])
        self.assertNotIn("fee", received[0])

    def test_no_algorand_submit_function_for_falcon(self):
        src = inspect.getsource(algo_anchor) + inspect.getsource(worker) + inspect.getsource(signer_client)
        self.assertNotIn("def send_if_allowed", src)
        self.assertNotIn("def _post_testnet", src)
        self.assertNotIn("testnet-api.algonode.cloud/v2/transactions", src)
        self.assertIn("def send_forbidden", inspect.getsource(algo_anchor))
        with self.assertRaises(RuntimeError):
            algo_anchor.send_forbidden({})

    def test_no_falcon_sk_secret_name_on_router(self):
        files = [
            Path(signer_client.__file__),
            Path(isolated_signer.__file__),
            Path(algo_anchor.__file__),
            Path(worker.__file__),
            Path(server.__file__),
        ]
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("LIVE402_PQ_FALCON_SK", text, path.name)
            self.assertNotIn("load_falcon_sk_from_env", text, path.name)

    def test_mainnet_genesis_rejected_on_leftover_path(self):
        note = algo_anchor.encode_note(ORIGIN, 1, b"\x11" * 32)
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = self.sender
        txn = algo_anchor.build_payment_txn(
            self.sender,
            note,
            {"genesisID": algo_anchor.MAINNET_GENESIS_ID, "genesisHash": algo_anchor.TESTNET_GENESIS_HASH},
        )
        txn["gen"] = algo_anchor.MAINNET_GENESIS_ID
        with self.assertRaises(algo_anchor.AnchorError):
            algo_anchor.validate_unsigned_anchor(txn)
        store.append(b"one")
        worker.save_anchor(0, 0)
        os.environ["LIVE402_PQ_SIGNER_TOKEN"] = _VECTOR_TOKEN
        with patch("socket.create_connection", side_effect=AssertionError("must not dial mainnet")) as dial:
            out = worker.maybe_submit(
                None,
                self.sender,
                {"genesisID": algo_anchor.MAINNET_GENESIS_ID},
                now=15 * 60,
            )
            dial.assert_not_called()
        self.assertIsNone(out)

    def test_http_boot_does_not_load_falcon_or_dial(self):
        os.environ.pop("LIVE402_PQ_SIGNER_TOKEN", None)
        with patch("socket.create_connection", side_effect=AssertionError("must not dial")) as dial:
            server.boot_http_process()
            dial.assert_not_called()
        src = inspect.getsource(server.boot_http_process)
        self.assertIn("boot_optional_log_signer", src)
        self.assertNotIn("falcon", src.lower())
        self.assertNotIn("LIVE402_PQ_FALCON_SK", inspect.getsource(server))

    def test_checkpoint_body_used_not_unsigned_txn(self):
        root = bytes.fromhex(_VECTOR_ROOT)
        body = ckpt.checkpoint_body(_VECTOR_ORIGIN, 1, root)
        self.assertEqual(body, _VECTOR_CHECKPOINT)
        payload = signer_client.build_request(
            origin=_VECTOR_ORIGIN,
            tree_size=1,
            root=root,
            consistency=[],
            timestamp=1,
            request_id="x",
            checkpoint=body,
            token=_VECTOR_TOKEN,
        )
        self.assertEqual(payload["checkpoint"], body)
        self.assertNotIn("txn", payload)


if __name__ == "__main__":
    unittest.main()
