"""6PN pq-anchor/1 client vs live signer 076825f reply shape.

TOKEN unset never dials. checkpoint is a signed-note. signed is SignedTxn hex.
TOKEN unset never dials. BROADCAST unset never POSTs. Homepage PQ
only after last_confirmed has a real TestNet txid.
"""

from __future__ import annotations

import base64
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


_VECTOR_TOKEN = "vector-token"
_VECTOR_ORIGIN = "402signal.com/pq/log"
_VECTOR_ROOT = "00" * 32
_VECTOR_BODY = (
    "402signal.com/pq/log\n1\nAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n"
)
# C2SP signed-note (type 0x01). Dummy 4-byte key id + 64-byte sig; parse_signed_note only.
_VECTOR_SIG_BLOB = base64.b64encode(b"\x00" * 4 + b"\x22" * 64).decode("ascii")
_VECTOR_CHECKPOINT = "%s\n%s %s %s\n" % (
    _VECTOR_BODY,
    ckpt.EMDASH,
    _VECTOR_ORIGIN,
    _VECTOR_SIG_BLOB,
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

# Mock SignedTxn bytes. Distinct from the live pqsig marker "present".
_MOCK_SIGNED_TXN = b"STXN-mock-not-pqsig" + bytes(range(32))


def _reply_line(*, tree_size=1, root=None, signed=None, pqsig=None):
    return json.dumps(
        {
            "ok": True,
            "tree_size": tree_size,
            "root": root or _VECTOR_ROOT,
            "pqsig": signer_client.PQSIG_PRESENT if pqsig is None else pqsig,
            "signed": (signed or _MOCK_SIGNED_TXN).hex(),
        },
        separators=(",", ":"),
    )


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
        ckpt.parse_signed_note(_VECTOR_CHECKPOINT)
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
        self.assertEqual(payload["checkpoint"], _VECTOR_CHECKPOINT)
        self.assertNotIn("checkpoint_body", payload)

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

    def test_request_sends_signed_note_and_exactly_protocol_keys(self):
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
        self.assertEqual(data["checkpoint"], _VECTOR_CHECKPOINT)
        ckpt.parse_signed_note(data["checkpoint"])
        self.assertNotIn("checkpoint_body", data)
        self.assertNotIn("checkpoint_body", line)
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
        extra["checkpoint_body"] = _VECTOR_BODY
        with self.assertRaises(signer_client.SignerClientError):
            signer_client.encode_request_line(extra)
        with self.assertRaises(signer_client.SignerClientError):
            signer_client.build_request(
                origin=_VECTOR_ORIGIN,
                tree_size=1,
                root=_VECTOR_ROOT,
                consistency=[],
                timestamp=1,
                request_id="x",
                checkpoint=_VECTOR_BODY,
                token=_VECTOR_TOKEN,
            )

    def test_timeout_is_25s(self):
        self.assertEqual(signer_client.IPC_TIMEOUT, 25.0)
        self.assertEqual(isolated_signer.IPC_TIMEOUT, 25.0)
        captured = []

        def fake_connect(addr, timeout=None):
            captured.append(timeout)
            raise OSError("nope")

        os.environ["LIVE402_PQ_SIGNER_TOKEN"] = _VECTOR_TOKEN
        with patch("socket.create_connection", side_effect=fake_connect):
            with self.assertRaises(signer_client.SignerClientError):
                signer_client.request_sign(
                    origin=_VECTOR_ORIGIN,
                    tree_size=1,
                    root=_VECTOR_ROOT,
                    consistency=[],
                    checkpoint=_VECTOR_CHECKPOINT,
                    host="127.0.0.1",
                    port=1,
                )
        self.assertEqual(captured, [25.0])

    def test_default_dial_is_6pn_9091_not_8080(self):
        os.environ.pop("LIVE402_PQ_SIGNER_HOST", None)
        os.environ.pop("LIVE402_PQ_SIGNER_PORT", None)
        self.assertEqual(signer_client.ipc_peer_host(), "402signal-pq-signer.internal")
        self.assertEqual(signer_client.ipc_port(), 9091)
        os.environ["LIVE402_PQ_SIGNER_PORT"] = "8080"
        with self.assertRaises(signer_client.SignerClientError):
            signer_client.ipc_port()
        os.environ.pop("LIVE402_PQ_SIGNER_PORT", None)

    def test_reply_shape_signed_is_signedtxn_not_pqsig(self):
        live = {
            "ok": True,
            "tree_size": 1,
            "root": _VECTOR_ROOT,
            "pqsig": "present",
            "signed": _MOCK_SIGNED_TXN.hex(),
        }
        parsed = signer_client.parse_reply(json.dumps(live, separators=(",", ":")))
        self.assertEqual(set(parsed), {"ok", "tree_size", "root", "pqsig", "signed"})
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["pqsig"], "present")
        self.assertEqual(parsed["signed"], _MOCK_SIGNED_TXN)
        self.assertIsInstance(parsed["signed"], bytes)
        self.assertNotEqual(parsed["signed"], b"present")
        self.assertNotEqual(parsed["signed"], parsed["pqsig"])
        self.assertEqual(signer_client.parse_reply(_reply_line())["pqsig"], "present")
        with self.assertRaises(signer_client.SignerClientError):
            signer_client.parse_reply(
                json.dumps(
                    {
                        "ok": True,
                        "tree_size": 1,
                        "root": _VECTOR_ROOT,
                        "pqsig": _MOCK_SIGNED_TXN.hex(),
                        "signed": _MOCK_SIGNED_TXN.hex(),
                    }
                )
            )
        with self.assertRaises(signer_client.SignerClientError):
            signer_client.parse_reply(
                json.dumps(
                    {
                        "ok": True,
                        "tree_size": 1,
                        "root": _VECTOR_ROOT,
                        "pqsig": "present",
                        "signed": b"present".hex(),
                    }
                )
            )
        with self.assertRaises(signer_client.SignerClientError):
            signer_client.parse_reply(
                json.dumps(
                    {
                        "ok": True,
                        "tree_size": 1,
                        "root": _VECTOR_ROOT,
                        "pqsig": "present",
                        "signed": "",
                    }
                )
            )
        with self.assertRaises(signer_client.SignerClientError):
            signer_client.parse_reply(
                json.dumps(
                    {
                        "ok": True,
                        "tree_size": 1,
                        "root": _VECTOR_ROOT,
                        "pqsig": "present",
                        "signed": "not-hex",
                    }
                )
            )
        with self.assertRaises(signer_client.SignerClientError):
            signer_client.parse_reply(json.dumps({"ok": True, "pqsig": "present"}))
        with self.assertRaises(signer_client.SignerClientError):
            signer_client.parse_reply(
                json.dumps(
                    {
                        "ok": True,
                        "tree_size": 1,
                        "root": _VECTOR_ROOT,
                        "pqsig": "Present",
                        "signed": _MOCK_SIGNED_TXN.hex(),
                    }
                )
            )

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
                    conn.sendall((_reply_line() + "\n").encode("utf-8"))
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
        self.assertEqual(out, _MOCK_SIGNED_TXN)
        self.assertNotEqual(out, b"present")
        self.assertNotEqual(out, "present")
        self.assertEqual(len(received), 1)
        self.assertEqual(set(received[0]), set(signer_client.REQUEST_KEYS))
        self.assertEqual(received[0]["hmac"], _VECTOR_MAC)
        self.assertEqual(received[0]["checkpoint"], _VECTOR_CHECKPOINT)
        self.assertNotIn("checkpoint_body", received[0])
        self.assertNotIn("txn", received[0])
        self.assertNotIn("fee", received[0])

    def _serve_mismatch(self, *, tree_size=None, root=None):
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
                    data = json.loads(raw.split(b"\n", 1)[0].decode("utf-8"))
                    received.append(data)
                    reply_size = data.get("tree_size") if tree_size is None else tree_size
                    reply_root = data.get("root") if root is None else root
                    conn.sendall(
                        (_reply_line(tree_size=reply_size, root=reply_root) + "\n").encode("utf-8")
                    )
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

    def test_request_sign_rejects_mismatched_tree_size(self):
        port, received = self._serve_mismatch(tree_size=2)
        os.environ["LIVE402_PQ_SIGNER_TOKEN"] = _VECTOR_TOKEN
        with self.assertRaises(signer_client.SignerClientError):
            signer_client.request_sign(
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
        self.assertEqual(len(received), 1)

    def test_request_sign_rejects_mismatched_root(self):
        port, received = self._serve_mismatch(root="ff" * 32)
        os.environ["LIVE402_PQ_SIGNER_TOKEN"] = _VECTOR_TOKEN
        with self.assertRaises(signer_client.SignerClientError):
            signer_client.request_sign(
                origin=_VECTOR_ORIGIN,
                tree_size=1,
                root=bytes.fromhex(_VECTOR_ROOT),
                consistency=[],
                checkpoint=_VECTOR_CHECKPOINT,
                now=1700000000,
                request_id="req-vector-1",
                host="127.0.0.1",
                port=port,
            )
        self.assertEqual(len(received), 1)

    def test_bind_reply_accepts_hex_or_bytes_root(self):
        parsed = signer_client.parse_reply(_reply_line(tree_size=1, root=_VECTOR_ROOT))
        bound = signer_client.bind_reply(parsed, tree_size=1, root=bytes.fromhex(_VECTOR_ROOT))
        self.assertEqual(bound["signed"], _MOCK_SIGNED_TXN)
        with self.assertRaises(signer_client.SignerClientError):
            signer_client.bind_reply(parsed, tree_size=9, root=_VECTOR_ROOT)
        with self.assertRaises(signer_client.SignerClientError):
            signer_client.bind_reply(parsed, tree_size=1, root="aa" * 32)

    def _serve_reply(self, received):
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
                    echo = _reply_line(tree_size=data.get("tree_size"), root=data.get("root"))
                    conn.sendall((echo + "\n").encode("utf-8"))
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
        return port

    def test_token_set_without_signed_checkpoint_never_dials(self):
        store.append(b"one")
        worker.save_anchor(0, 0)
        os.environ["LIVE402_PQ_SIGNER_TOKEN"] = _VECTOR_TOKEN
        params = {"genesisID": algo_anchor.TESTNET_GENESIS_ID}
        with patch("socket.create_connection", side_effect=AssertionError("must not dial")) as dial:
            out = worker.maybe_submit(None, self.sender, params, now=15 * 60)
            dial.assert_not_called()
        self.assertIsNone(out)
        store.save_checkpoint(1, _VECTOR_BODY)
        with patch("socket.create_connection", side_effect=AssertionError("must not dial")) as dial:
            out = worker.maybe_submit(None, self.sender, params, now=15 * 60)
            dial.assert_not_called()
        self.assertIsNone(out)

    def test_worker_sends_signed_note_as_checkpoint(self):
        received = []
        port = self._serve_reply(received)
        store.append(b"one")
        store.save_checkpoint(1, _VECTOR_CHECKPOINT)
        worker.save_anchor(0, 0)
        os.environ["LIVE402_PQ_SIGNER_TOKEN"] = _VECTOR_TOKEN
        os.environ["LIVE402_PQ_SIGNER_HOST"] = "127.0.0.1"
        os.environ["LIVE402_PQ_SIGNER_PORT"] = str(port)
        out = worker.maybe_submit(
            None,
            self.sender,
            {"genesisID": algo_anchor.TESTNET_GENESIS_ID},
            now=15 * 60,
        )
        self.assertIsNotNone(out)
        self.assertEqual(out["signed"], _MOCK_SIGNED_TXN)
        self.assertNotEqual(out["signed"], b"present")
        self.assertNotEqual(out["signed"], "present")
        self.assertFalse(out["submitted"])
        self.assertTrue(out["authorized"])
        self.assertFalse(out["confirmed"])
        self.assertEqual(worker.last_authorized()["size"], 1)
        self.assertEqual(worker.last_confirmed()["size"], 0)
        self.assertEqual(worker.last_anchor()["size"], 0)
        self.assertIsNone(worker.public_anchor())
        self.assertEqual(len(received), 1)
        self.assertEqual(set(received[0]), set(signer_client.REQUEST_KEYS))
        self.assertEqual(received[0]["checkpoint"], _VECTOR_CHECKPOINT)
        ckpt.parse_signed_note(received[0]["checkpoint"])
        self.assertNotIn("checkpoint_body", received[0])
        for key in ("fee", "firstValid", "sender", "amount", "txn", "unsigned"):
            self.assertNotIn(key, received[0])

    def test_send_forbidden_stays_default_and_broadcast_is_gated(self):
        src = inspect.getsource(algo_anchor) + inspect.getsource(worker) + inspect.getsource(signer_client)
        self.assertIn("def send_if_allowed", src)
        self.assertIn("def _post_testnet", src)
        self.assertIn("Never posts MainNet", inspect.getsource(algo_anchor.send_if_allowed))
        self.assertNotIn("submit_mainnet_canary", inspect.getsource(worker))
        self.assertIn("def send_forbidden", inspect.getsource(algo_anchor))
        with self.assertRaises(RuntimeError):
            algo_anchor.send_forbidden({})
        os.environ.pop("LIVE402_PQ_FALCON_BROADCAST", None)
        posted = []
        self.assertIsNone(
            algo_anchor.send_if_allowed(
                _MOCK_SIGNED_TXN,
                send_fn=lambda blob: posted.append(blob) or "B" * 52,
            )
        )
        self.assertEqual(posted, [])

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
        self.assertNotIn("LIVE402_PQ_FALCON_SK", inspect.getsource(server))
        self.assertNotIn("load_falcon_sk_from_env", inspect.getsource(server))

    def test_homepage_omits_pq_without_confirmed_anchor(self):
        home = Path(__file__).resolve().parent.parent.joinpath("live402", "static", "index.html")
        text = home.read_text(encoding="utf-8")
        self.assertNotIn("Trust the history", text)
        self.assertNotIn("View latest TestNet anchor", text)
        self.assertNotIn("View TestNet transaction", text)
        self.assertNotIn("placeholder", text.lower())
        self.assertNotIn("YOUR_TXID", text)
        self.assertIn("PQ Trust", text)
        self.assertNotIn("PQ transparency", text)
        self.assertIsNone(worker.public_anchor())
        self.assertEqual(worker.homepage_pq_html(), "")


if __name__ == "__main__":
    unittest.main()
