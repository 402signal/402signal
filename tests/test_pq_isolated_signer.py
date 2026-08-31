"""Isolated Falcon signer process: unsigned txn in, pqsig out. No /route. No log SK."""

from __future__ import annotations

import inspect
import json
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import payment, server
from live402.pq import algo_anchor, isolated_signer, receipt, store


class IsolatedSignerProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        receipt.configure_signer(None)
        algo_anchor.configure_falcon_sk(None)
        os.environ.pop("LIVE402_PQ_FALCON_SK", None)
        os.environ.pop("LIVE402_PQ_LOG_SK", None)
        os.environ.pop("FLY_PROCESS_GROUP", None)
        self.sk = os.urandom(algo_anchor.FALCON_SK_LEN)
        self.hex_sk = self.sk.hex()
        self.log_seed = os.urandom(32)
        self.log_hex = self.log_seed.hex()
        self.sender = payment.DEFAULT_PAYTO_ALGORAND
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        receipt.configure_signer(None)
        algo_anchor.configure_falcon_sk(None)
        store.reset()
        os.environ.pop("LIVE402_PQ_FALCON_SK", None)
        os.environ.pop("LIVE402_PQ_LOG_SK", None)
        os.environ.pop("FLY_PROCESS_GROUP", None)
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

    def test_does_not_load_log_sk_even_when_present(self):
        os.environ["LIVE402_PQ_LOG_SK"] = self.log_hex
        os.environ["LIVE402_PQ_FALCON_SK"] = self.hex_sk
        receipt.configure_signer(None)
        isolated_signer.boot()
        self.assertIsNone(receipt.current_signer())
        self.assertEqual(algo_anchor.current_falcon_sk(), self.sk)
        src = Path(isolated_signer.__file__).read_text(encoding="utf-8")
        self.assertNotIn("load_signer_from_env", src)
        self.assertNotIn("boot_optional_log_signer", src)
        self.assertNotIn("handle_route", src)
        self.assertNotIn("ThreadingHTTPServer", src)

    def test_main_empty_stdin_does_not_serve_http_or_configure_log(self):
        os.environ["LIVE402_PQ_LOG_SK"] = self.log_hex
        os.environ["LIVE402_PQ_FALCON_SK"] = self.hex_sk
        receipt.configure_signer(None)
        with patch("sys.stdin", StringIO("")):
            with patch("sys.stdout", StringIO()) as out:
                isolated_signer.main()
        self.assertIsNone(receipt.current_signer())
        self.assertNotIn(self.hex_sk, out.getvalue())
        self.assertNotIn(self.log_hex, out.getvalue())

    def test_refuses_to_run_in_app_process_group(self):
        os.environ["FLY_PROCESS_GROUP"] = "app"
        with self.assertRaises(isolated_signer.SignerProcessError):
            isolated_signer.boot()

    def test_http_main_refuses_falcon_process_group(self):
        os.environ["FLY_PROCESS_GROUP"] = "falcon"
        with self.assertRaises(SystemExit) as ctx:
            server.main(["--host", "127.0.0.1", "--port", "0"])
        self.assertIn("falcon", str(ctx.exception).lower())

    def test_source_has_no_route_server(self):
        src = inspect.getsource(isolated_signer)
        self.assertIn("unsigned txn in", src.lower())
        self.assertNotIn("handle_route", src)
        self.assertNotIn('"/route"', src)
        self.assertNotIn("SimpleHTTPRequestHandler", src)
        falcon_main = inspect.getsource(isolated_signer.main)
        self.assertNotIn("boot_optional_log_signer", falcon_main)

    def test_app_process_still_send_forbidden(self):
        with self.assertRaises(RuntimeError):
            algo_anchor.send_forbidden({})
        src = inspect.getsource(server.main)
        self.assertNotIn("boot_optional_falcon_sk", src)


if __name__ == "__main__":
    unittest.main()
