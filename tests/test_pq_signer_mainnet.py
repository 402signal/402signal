"""MainNet signer client isolation. No TestNet fallback. No live network."""

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
from live402.pq import ORIGIN, ORIGIN_MAINNET, algo_anchor, signer_client, signer_mainnet, store
from live402.pq import checkpoint as ckpt
from tests.pq_test_env import clear_pq_env


_TOKEN = "mainnet-vector-token"
_SIG = base64.b64encode(b"\x00" * 4 + b"\x22" * 64).decode("ascii")


def _signed_note(size, root, origin=ORIGIN_MAINNET):
    body = ckpt.checkpoint_body(origin, int(size), bytes(root))
    return "%s\n%s %s %s\n" % (body, ckpt.EMDASH, origin, _SIG)


def _policy(now=1_700_000_000, last_round=1, fee=3000):
    return {
        "last_round": last_round,
        "min_fee": 1000,
        "fee_per_byte": 0,
        "fv": last_round,
        "lv": last_round + 1000,
        "canonical_fee": fee,
        "snapshot_at": now,
        "size_rule": "deterministic_falcon_envelope_estimate",
    }


def _mainnet_signed(size, root, addr=None, fee=3000, fv=1, lv=1001):
    from live402 import algo_tx

    addr = addr or payment.DEFAULT_PAYTO_ALGORAND
    note = algo_anchor.encode_note(ORIGIN_MAINNET, int(size), bytes(root))
    gh = base64.b64decode(algo_anchor.MAINNET_GENESIS_HASH)
    txn = algo_tx.pay_txn(addr, addr, 0, fee, fv, lv, algo_anchor.MAINNET_GENESIS_ID, gh, note=note)
    return algo_tx.msgpack_encode(
        {
            "pqsig": {
                "pk": b"pk" + bytes(range(14)),
                "sch": "f1",
                "sig": b"sig" + bytes(range(29)),
                "slt": 0,
            },
            "txn": txn,
        }
    )


class MainNetSignerIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clear_pq_env()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log-mainnet.sqlite")
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN_MAINNET
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = payment.DEFAULT_PAYTO_ALGORAND
        store.reset()
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
        clear_pq_env()
        store.reset()
        self.tmp.cleanup()

    def test_defaults_are_mainnet_only(self):
        self.assertEqual(signer_mainnet.DEFAULT_IPC_HOST, "402signal-pq-signer-mainnet.internal")
        self.assertEqual(signer_mainnet.DEFAULT_IPC_PORT, 9091)
        self.assertEqual(signer_mainnet.TOKEN_ENV, "LIVE402_PQ_SIGNER_MAINNET_TOKEN")
        self.assertNotEqual(signer_mainnet.DEFAULT_IPC_HOST, signer_client.DEFAULT_IPC_HOST)
        self.assertEqual(signer_client.DEFAULT_IPC_HOST, "402signal-pq-signer.internal")

    def test_testnet_token_does_not_enable_mainnet_client(self):
        os.environ["LIVE402_PQ_SIGNER_TOKEN"] = "testnet-only"
        os.environ.pop("LIVE402_PQ_SIGNER_MAINNET_TOKEN", None)
        self.assertFalse(signer_mainnet.token_configured())
        with patch("socket.create_connection", side_effect=AssertionError("must not dial")) as dial:
            with self.assertRaises(signer_mainnet.SignerClientError):
                signer_mainnet.request_sign(
                    origin=ORIGIN_MAINNET,
                    tree_size=1,
                    root=b"\x11" * 32,
                    consistency=[],
                    checkpoint=_signed_note(1, b"\x11" * 32),
                    policy=_policy(),
                )
            dial.assert_not_called()

    def test_testnet_host_env_never_used(self):
        os.environ["LIVE402_PQ_SIGNER_MAINNET_TOKEN"] = _TOKEN
        os.environ["LIVE402_PQ_SIGNER_HOST"] = "testnet-should-not-win.internal"
        os.environ["LIVE402_PQ_SIGNER_PORT"] = "1"
        os.environ.pop("LIVE402_PQ_SIGNER_MAINNET_HOST", None)
        os.environ.pop("LIVE402_PQ_SIGNER_MAINNET_PORT", None)
        self.assertEqual(signer_mainnet.ipc_peer_host(), "402signal-pq-signer-mainnet.internal")
        self.assertEqual(signer_mainnet.ipc_port(), 9091)
        src = __import__("pathlib").Path(signer_mainnet.__file__).read_text(encoding="utf-8")
        self.assertIn("must not read testnet signer env", src)
        self.assertIn("testnet signer host forbidden", src)
        self.assertIn("LIVE402_PQ_SIGNER_MAINNET_TOKEN", src)

    def test_explicit_testnet_host_rejected(self):
        os.environ["LIVE402_PQ_SIGNER_MAINNET_TOKEN"] = _TOKEN
        with patch("socket.create_connection", side_effect=AssertionError("must not dial")) as dial:
            with self.assertRaises(signer_mainnet.SignerClientError):
                signer_mainnet.request_sign(
                    origin=ORIGIN_MAINNET,
                    tree_size=1,
                    root=b"\x11" * 32,
                    consistency=[],
                    checkpoint=_signed_note(1, b"\x11" * 32),
                    policy=_policy(),
                    host="402signal-pq-signer.internal",
                    port=9091,
                )
            dial.assert_not_called()

    def test_origin_must_be_mainnet(self):
        os.environ["LIVE402_PQ_SIGNER_MAINNET_TOKEN"] = _TOKEN
        with patch("socket.create_connection", side_effect=AssertionError("must not dial")) as dial:
            with self.assertRaises(signer_mainnet.SignerClientError):
                signer_mainnet.request_sign(
                    origin=ORIGIN,
                    tree_size=1,
                    root=b"\x11" * 32,
                    consistency=[],
                    checkpoint=_signed_note(1, b"\x11" * 32, origin=ORIGIN),
                    policy=_policy(),
                )
            dial.assert_not_called()

    def _serve(self, signed, received):
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
                    reply = json.dumps(
                        {
                            "ok": True,
                            "tree_size": data.get("tree_size"),
                            "root": data.get("root"),
                            "pqsig": "present",
                            "signed": signed.hex(),
                        },
                        separators=(",", ":"),
                    )
                    conn.sendall((reply + "\n").encode("utf-8"))
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

    def test_round_trip_binds_and_verifies(self):
        root = b"\x11" * 32
        blob = _mainnet_signed(1, root)
        received = []
        port = self._serve(blob, received)
        os.environ["LIVE402_PQ_SIGNER_MAINNET_TOKEN"] = _TOKEN
        os.environ["LIVE402_PQ_SIGNER_TOKEN"] = "must-not-be-used"
        note = _signed_note(1, root)
        out = signer_mainnet.request_signed(
            origin=ORIGIN_MAINNET,
            tree_size=1,
            root=root,
            consistency=[],
            checkpoint=note,
            policy=_policy(),
            host="127.0.0.1",
            port=port,
            params={"minFee": 1000, "fee": 0, "lastRound": 1},
        )
        self.assertEqual(out["signed"], blob)
        self.assertEqual(out["verified"]["fee"], 3000)
        self.assertEqual(set(received[0]), set(signer_mainnet.REQUEST_KEYS))
        self.assertEqual(received[0]["v"], 2)
        self.assertIn("policy", received[0])
        self.assertEqual(received[0]["policy"]["canonical_fee"], 3000)
        for key in ("fee", "sender", "amount", "txn", "unsigned", "pk", "sk"):
            self.assertNotIn(key, received[0])

    def test_arbitrary_fee_rejected(self):
        root = b"\x11" * 32
        blob = _mainnet_signed(1, root, fee=5000)
        received = []
        port = self._serve(blob, received)
        os.environ["LIVE402_PQ_SIGNER_MAINNET_TOKEN"] = _TOKEN
        with self.assertRaises(algo_anchor.AnchorError):
            signer_mainnet.request_signed(
                origin=ORIGIN_MAINNET,
                tree_size=1,
                root=root,
                consistency=[],
                checkpoint=_signed_note(1, root),
                policy=_policy(),
                host="127.0.0.1",
                port=port,
                params={"minFee": 1000, "fee": 0, "lastRound": 1},
            )

    def test_protocol_probe_does_not_create_auth(self):
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
                    received.append(raw)
                    conn.sendall(b'{"ok":false,"error":"hmac"}\n')
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
        out = signer_mainnet.protocol_probe(host="127.0.0.1", port=port)
        self.assertTrue(out["reachable"])
        self.assertTrue(out["protocol"])
        self.assertTrue(out["hmac_rejected"])
        self.assertEqual(out["canonical"], "pq-anchor/2")
        self.assertEqual(store.size(), 0)
        self.assertFalse(store.last_authorized_checkpoint().get("signed"))
        self.assertIn(b"unsigned", received[0])
        blob = str(out).lower()
        self.assertNotIn("mnemonic", blob)
        self.assertNotIn(_TOKEN.lower(), blob)


if __name__ == "__main__":
    unittest.main()
