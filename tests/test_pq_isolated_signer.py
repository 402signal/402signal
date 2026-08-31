"""Isolated Falcon signer process: unsigned txn in, pqsig out. No /route. No log SK."""

from __future__ import annotations

import inspect
import json
import os
import socket
import tempfile
import threading
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import payment, server
from live402.pq import algo_anchor, isolated_signer, receipt, store, worker


class IsolatedSignerProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        worker.clear_queue()
        receipt.configure_signer(None)
        algo_anchor.configure_falcon_sk(None)
        os.environ.pop("LIVE402_PQ_FALCON_SK", None)
        os.environ.pop("LIVE402_PQ_LOG_SK", None)
        os.environ.pop("FLY_PROCESS_GROUP", None)
        os.environ.pop("LIVE402_PQ_SIGNER_HOST", None)
        os.environ.pop("LIVE402_PQ_SIGNER_PORT", None)
        os.environ.pop("LIVE402_PQ_SIGNER_BIND", None)
        os.environ.pop("LIVE402_PQ_FALCON_NETWORK", None)
        os.environ.pop("LIVE402_PQ_FALCON_BROADCAST", None)
        os.environ.pop("LIVE402_PQ_FALCON_ADDRESS", None)
        self.sk = os.urandom(algo_anchor.FALCON_SK_LEN)
        self.hex_sk = self.sk.hex()
        self.log_seed = os.urandom(32)
        self.log_hex = self.log_seed.hex()
        self.sender = payment.DEFAULT_PAYTO_ALGORAND
        self._stop = False
        self._threads = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self._stop = True
        for thread in self._threads:
            thread.join(timeout=2)
        receipt.configure_signer(None)
        algo_anchor.configure_falcon_sk(None)
        worker.clear_queue()
        store.reset()
        os.environ.pop("LIVE402_PQ_FALCON_SK", None)
        os.environ.pop("LIVE402_PQ_LOG_SK", None)
        os.environ.pop("FLY_PROCESS_GROUP", None)
        os.environ.pop("LIVE402_PQ_SIGNER_HOST", None)
        os.environ.pop("LIVE402_PQ_SIGNER_PORT", None)
        os.environ.pop("LIVE402_PQ_SIGNER_BIND", None)
        os.environ.pop("LIVE402_PQ_FALCON_NETWORK", None)
        os.environ.pop("LIVE402_PQ_FALCON_BROADCAST", None)
        os.environ.pop("LIVE402_PQ_FALCON_ADDRESS", None)
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        self.tmp.cleanup()

    def _unsigned(self):
        note = algo_anchor.encode_note("402signal.com/pq/log", 1, b"\x11" * 32)
        return algo_anchor.build_payment_txn(
            self.sender,
            note,
            {
                "genesisID": algo_anchor.TESTNET_GENESIS_ID,
                "genesisHash": algo_anchor.TESTNET_GENESIS_HASH,
                "firstValid": 1,
                "lastValid": 1001,
                "fee": 3000,
            },
        )

    def _start_ipc(self, signer_callback=None, host="127.0.0.1"):
        sock = isolated_signer.bind_ipc(host, 0)
        port = sock.getsockname()[1]
        self._stop = False
        thread = threading.Thread(
            target=isolated_signer.serve_ipc,
            args=(sock, signer_callback, lambda: self._stop),
            daemon=True,
        )
        thread.start()
        self._threads.append(thread)
        return sock, port

    def test_unsigned_in_pqsig_out_via_callback(self):
        txn = self._unsigned()
        line = isolated_signer.encode_unsigned_message(txn)
        self.assertNotIn(self.hex_sk, line)
        out = StringIO()
        isolated_signer.run_loop(StringIO(line + "\n"), out, signer_callback=lambda _t: b"pqsig-iso")
        payload = json.loads(out.getvalue().strip())
        self.assertEqual(payload["pqsig"], b"pqsig-iso".hex())
        self.assertNotIn("error", payload)
        self.assertNotIn(self.hex_sk, out.getvalue())

    def test_ipc_never_echoes_sk(self):
        os.environ["LIVE402_PQ_FALCON_SK"] = self.hex_sk
        isolated_signer.boot()
        self.assertEqual(algo_anchor.current_falcon_sk(), self.sk)
        _sock, port = self._start_ipc(signer_callback=lambda _t: b"pqsig-ipc")
        txn = self._unsigned()
        pqsig = isolated_signer.request_pqsig(txn, host="127.0.0.1", port=port)
        self.assertEqual(pqsig, b"pqsig-ipc")
        line = isolated_signer.encode_unsigned_message(txn)
        with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
            conn.sendall((line + "\n").encode("utf-8"))
            raw = conn.recv(4096)
        self.assertNotIn(self.hex_sk.encode("ascii"), raw)
        self.assertNotIn(self.sk, raw)
        self.assertNotIn(b"BEGIN PRIVATE KEY", raw)
        payload = json.loads(raw.decode("utf-8").strip())
        self.assertEqual(payload["pqsig"], b"pqsig-ipc".hex())
        self.assertNotIn("sk", payload)
        self.assertNotIn(self.hex_sk, json.dumps(payload))

    def test_does_not_load_log_sk_even_when_present(self):
        os.environ["LIVE402_PQ_LOG_SK"] = self.log_hex
        os.environ["LIVE402_PQ_FALCON_SK"] = self.hex_sk
        os.environ["FLY_PROCESS_GROUP"] = isolated_signer.PROCESS_NAME
        receipt.configure_signer(None)
        isolated_signer.boot()
        self.assertIsNone(receipt.current_signer())
        self.assertEqual(algo_anchor.current_falcon_sk(), self.sk)
        src = Path(isolated_signer.__file__).read_text(encoding="utf-8")
        self.assertNotIn("load_signer_from_env", src)
        self.assertNotIn("boot_optional_log_signer", src)
        self.assertNotIn("handle_route", src)
        self.assertNotIn("ThreadingHTTPServer", src)
        self.assertIn("FLY_PROCESS_GROUP", src)
        self.assertIn("falcon process", src)

    def test_isolated_boot_loads_falcon_sk_not_log_sk(self):
        os.environ["LIVE402_PQ_LOG_SK"] = self.log_hex
        os.environ["LIVE402_PQ_FALCON_SK"] = self.hex_sk
        receipt.configure_signer(None)
        algo_anchor.configure_falcon_sk(None)
        self.assertTrue(isolated_signer.boot())
        self.assertEqual(algo_anchor.current_falcon_sk(), self.sk)
        self.assertIsNone(receipt.current_signer())

    def test_http_boot_does_not_load_falcon_sk_even_when_env_set(self):
        os.environ["LIVE402_PQ_FALCON_SK"] = self.hex_sk
        os.environ["FLY_PROCESS_GROUP"] = isolated_signer.APP_PROCESS_NAME
        receipt.configure_signer(None)
        algo_anchor.configure_falcon_sk(None)
        server.boot_http_process()
        self.assertIsNone(algo_anchor.current_falcon_sk())
        self.assertIsNone(receipt.current_signer())

    def test_refuses_to_run_in_app_process_group(self):
        os.environ["FLY_PROCESS_GROUP"] = isolated_signer.APP_PROCESS_NAME
        with self.assertRaises(isolated_signer.SignerProcessError):
            isolated_signer.boot()

    def test_http_main_refuses_falcon_process_group(self):
        os.environ["FLY_PROCESS_GROUP"] = isolated_signer.PROCESS_NAME
        with self.assertRaises(SystemExit) as ctx:
            server.main(["--host", "127.0.0.1", "--port", "0"])
        self.assertIn("falcon", str(ctx.exception).lower())

    def test_source_has_no_route_or_http_server(self):
        src = inspect.getsource(isolated_signer)
        self.assertIn("unsigned txn in", src.lower())
        self.assertNotIn("handle_route", src)
        self.assertNotIn('"/route"', src)
        self.assertNotIn("SimpleHTTPRequestHandler", src)
        self.assertNotIn("ThreadingHTTPServer", src)
        self.assertIn("FLY_PROCESS_GROUP", src)
        lib_main = inspect.getsource(isolated_signer.main)
        self.assertNotIn("boot_optional_log_signer", lib_main)
        self.assertNotIn("PORT", lib_main)

    def test_signer_ignores_port_8080(self):
        os.environ["PORT"] = "8080"
        os.environ["LIVE402_PQ_SIGNER_PORT"] = "8080"
        with self.assertRaises(isolated_signer.SignerProcessError):
            isolated_signer.ipc_port()
        with self.assertRaises(isolated_signer.SignerProcessError):
            isolated_signer.bind_ipc("127.0.0.1", 8080)
        os.environ.pop("LIVE402_PQ_SIGNER_PORT", None)
        self.assertEqual(isolated_signer.ipc_port(), isolated_signer.DEFAULT_IPC_PORT)
        self.assertNotEqual(isolated_signer.ipc_port(), 8080)
        os.environ.pop("PORT", None)

    def test_app_still_send_forbidden(self):
        with self.assertRaises(RuntimeError):
            algo_anchor.send_forbidden({})
        src = inspect.getsource(server.main)
        self.assertNotIn("boot_optional_falcon_sk", src)
        self.assertNotIn("load_falcon_sk_from_env", src)
        self.assertIn("FLY_PROCESS_GROUP", inspect.getsource(server.boot_http_process))
        self.assertIn("falcon process", inspect.getsource(server.boot_http_process))

    def test_missing_signer_does_not_send(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = self.sender
        os.environ["LIVE402_PQ_SIGNER_HOST"] = "127.0.0.1"
        os.environ["LIVE402_PQ_SIGNER_PORT"] = "1"
        store.append(b"one")
        worker.save_anchor(0, 0)
        sent = []

        def mock_send(blob):
            sent.append(blob)
            raise AssertionError("must not send when signer is down")

        out = worker.maybe_submit(
            None,
            self.sender,
            {
                "genesisID": algo_anchor.TESTNET_GENESIS_ID,
                "genesisHash": algo_anchor.TESTNET_GENESIS_HASH,
                "firstValid": 1,
                "lastValid": 1001,
                "fee": 3000,
            },
            now=15 * 60,
            send_fn=mock_send,
        )
        self.assertIsNone(out)
        self.assertEqual(sent, [])

    def test_mainnet_genesis_still_rejected_via_ipc_callback(self):
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "testnet"
        os.environ["LIVE402_PQ_FALCON_BROADCAST"] = "1"
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = self.sender
        store.append(b"one")
        worker.save_anchor(0, 0)
        sent = []

        def mock_send(blob):
            sent.append(blob)
            raise AssertionError("must not send mainnet")

        params = {
            "genesisID": algo_anchor.MAINNET_GENESIS_ID,
            "genesisHash": algo_anchor.TESTNET_GENESIS_HASH,
            "firstValid": 1,
            "lastValid": 1001,
            "fee": 3000,
        }
        out = worker.maybe_submit(
            lambda _u: b"sig",
            self.sender,
            params,
            now=15 * 60,
            send_fn=mock_send,
        )
        self.assertIsNone(out)
        self.assertEqual(sent, [])
        self.assertFalse(
            algo_anchor.submit_allowed(
                signer_callback=lambda _u: b"sig",
                sender=self.sender,
                params=params,
            )
        )


if __name__ == "__main__":
    unittest.main()
