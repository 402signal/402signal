"""Lock transparency explorer/network semantics and TestNet/MainNet UI isolation.

TRANSPARENCY_NETWORK_AWARE and TESTNET_MAINNET_UI_ISOLATION are the
named suites this cleanup must keep green. Explorer URLs come from
independently confirmed anchor state. MainNet secrets never imply
MainNet confirmation. AUTHORIZED/SUBMITTED never render as CONFIRMED.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import payment
from live402.pq import ORIGIN, ORIGIN_MAINNET, algo_anchor, store, worker
from live402.pq import transparency as pq_view
from live402.server import Handler
from tests.pq_test_env import clear_pq_env

_TX_A = "B" * 52
_TX_B = "C" * 52
_FALCON_TESTNET = "OBHYXCUVOLSTZVBN5JUFIYBD4X4ZFIAFZMWMU2P45VBYGWT26MV34IFFIU"
_MAINNET_TOKEN_NAME = "named-not-valued"
_UNKNOWN_ORIGIN = "example.invalid/pq/log/unknown"


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


def _get(port, path="/transparency"):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    return res.status, raw.decode("utf-8")


def _confirm(size, root, txid, rnd, at, origin=ORIGIN, network="", genesis_id=""):
    store.save_confirmed_checkpoint(
        tree_size=size,
        origin=origin,
        root=root,
        txid=txid,
        confirmed_round=rnd,
        at=at,
        network=network,
        genesis_id=genesis_id,
    )


class TRANSPARENCY_NETWORK_AWARE(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clear_pq_env()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = _FALCON_TESTNET
        store.reset()
        worker.clear_queue()
        self.httpd, self.port = _serve()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        worker.clear_queue()
        store.reset()
        clear_pq_env()
        os.environ.pop("LIVE402_PQ_FALCON_ADDRESS", None)
        self.tmp.cleanup()

    def _html(self, path="/transparency"):
        status, html = _get(self.port, path)
        self.assertEqual(status, 200, path)
        return html

    def test_confirmed_testnet_anchor_uses_testnet_explorer(self):
        store.append(b"one")
        _confirm(1, store.root(1), _TX_A, 99, 1_700_000_100)
        html = self._html()
        self.assertIn("Confirmed", html)
        self.assertIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertIn("(Pera Explorer, TestNet)", html)
        self.assertIn(algo_anchor.TESTNET_INDEXER_TXN_URL + _TX_A, html)
        self.assertIn("View raw TestNet transaction JSON", html)
        self.assertNotIn(algo_anchor.MAINNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertNotIn("(Pera Explorer, MainNet)", html)
        self.assertNotIn("\N{EM DASH}", html)
        self.assertIn(
            "does not make Base or Solana merchant payments post-quantum secure",
            html,
        )
        self.assertIn("The Algorand transaction authorizes a checkpoint.", html)
        self.assertIn("It is not a merchant payment.", html)

    def test_confirmed_mainnet_anchor_uses_verified_mainnet_explorer_only(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log-mainnet.sqlite")
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN_MAINNET
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = payment.DEFAULT_PAYTO_ALGORAND
        os.environ["LIVE402_PQ_SIGNER_MAINNET_TOKEN"] = _MAINNET_TOKEN_NAME
        store.reset()
        store.append(b"one")
        _confirm(
            1,
            store.root(1),
            _TX_A,
            200,
            1_700_000_200,
            origin=ORIGIN_MAINNET,
            network="mainnet",
            genesis_id=algo_anchor.MAINNET_GENESIS_ID,
        )
        self.httpd, self.port = _serve()
        html = self._html()
        self.assertIn(algo_anchor.MAINNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertIn("(Pera Explorer, MainNet)", html)
        self.assertIn(algo_anchor.MAINNET_INDEXER_TXN_URL + _TX_A, html)
        self.assertIn("View raw MainNet transaction JSON", html)
        self.assertNotIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertNotIn("testnet.explorer.perawallet.app/tx/" + _TX_A, html)
        self.assertNotIn("(Pera Explorer, TestNet)", html)
        self.assertEqual(
            algo_anchor.verified_explorer_tx_url(_TX_A, "mainnet"),
            algo_anchor.MAINNET_EXPLORER_TX_URL + _TX_A,
        )
        self.assertTrue(
            algo_anchor.verified_explorer_tx_url(_TX_A, "mainnet").startswith(
                "https://explorer.perawallet.app/tx/"
            )
        )
        self.assertNotIn("testnet", algo_anchor.verified_explorer_tx_url(_TX_A, "mainnet"))

    def test_mainnet_origin_without_genesis_suppresses_explorer(self):
        store.append(b"one")
        _confirm(1, store.root(1), _TX_A, 12, 1_700_000_300, origin=ORIGIN_MAINNET)
        html = self._html()
        self.assertIn("Confirmed", html)
        self.assertNotIn(algo_anchor.MAINNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertNotIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertNotIn("View latest anchor on Pera", html)
        self.assertEqual(pq_view.confirmed_view()["explorer"], "")
        self.assertEqual(pq_view.confirmed_view()["network"], "")

    def test_unknown_origin_suppresses_explorer_rather_than_guess(self):
        store.append(b"one")
        _confirm(1, store.root(1), _TX_A, 13, 1_700_000_400, origin=_UNKNOWN_ORIGIN)
        html = self._html()
        self.assertIn("Confirmed", html)
        self.assertNotIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertNotIn(algo_anchor.MAINNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertEqual(algo_anchor.confirmed_anchor_network({"origin": _UNKNOWN_ORIGIN}), "")
        self.assertEqual(algo_anchor.verified_explorer_tx_url(_TX_A, ""), "")
        self.assertEqual(algo_anchor.verified_explorer_tx_url(_TX_A, "prod"), "")

    def test_helper_urls_require_explicit_network(self):
        self.assertEqual(pq_view.pera_tx_url(_TX_A), "")
        self.assertEqual(pq_view.indexer_tx_url(_TX_A), "")
        self.assertEqual(
            pq_view.pera_tx_url(_TX_A, "testnet"),
            algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A,
        )
        self.assertEqual(
            pq_view.pera_tx_url(_TX_A, "mainnet"),
            algo_anchor.MAINNET_EXPLORER_TX_URL + _TX_A,
        )
        self.assertEqual(algo_anchor.explorer_hint_label(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A), "TestNet")
        self.assertEqual(algo_anchor.explorer_hint_label(algo_anchor.MAINNET_EXPLORER_TX_URL + _TX_A), "MainNet")
        self.assertEqual(algo_anchor.explorer_hint_label("https://example.test/tx/" + _TX_A), "")


class TESTNET_MAINNET_UI_ISOLATION(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clear_pq_env()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = _FALCON_TESTNET
        store.reset()
        worker.clear_queue()
        self.httpd, self.port = _serve()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        worker.clear_queue()
        store.reset()
        clear_pq_env()
        os.environ.pop("LIVE402_PQ_FALCON_ADDRESS", None)
        self.tmp.cleanup()

    def _html(self, path="/transparency"):
        status, html = _get(self.port, path)
        self.assertEqual(status, 200, path)
        return html

    def _restart_with_mainnet_identity(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        os.environ["LIVE402_PQ_FALCON_NETWORK"] = "mainnet"
        os.environ["LIVE402_PQ_LOG_EPOCH"] = "mainnet-v1"
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log-mainnet.sqlite")
        os.environ["LIVE402_PQ_LOG_ORIGIN"] = ORIGIN_MAINNET
        os.environ["LIVE402_PQ_FALCON_MAINNET_ADDRESS"] = payment.DEFAULT_PAYTO_ALGORAND
        os.environ["LIVE402_PQ_SIGNER_MAINNET_TOKEN"] = _MAINNET_TOKEN_NAME
        store.reset()
        self.httpd, self.port = _serve()

    def test_mainnet_secrets_do_not_make_testnet_anchor_mainnet(self):
        for key in (
            "LIVE402_PQ_SIGNER_MAINNET_TOKEN",
            "LIVE402_PQ_CONFIRM_TATUM_API_KEY",
            "LIVE402_PQ_CONFIRM_INDEXER_TOKEN",
        ):
            os.environ[key] = _MAINNET_TOKEN_NAME
        store.append(b"one")
        _confirm(1, store.root(1), _TX_A, 44, 1_700_000_500)
        html = self._html()
        self.assertIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertIn("(Pera Explorer, TestNet)", html)
        self.assertNotIn(algo_anchor.MAINNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertNotIn("(Pera Explorer, MainNet)", html)
        home = self._html("/")
        self.assertNotIn(algo_anchor.MAINNET_EXPLORER_TX_URL + _TX_A, home)
        self.assertNotIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A, home)
        for key in (
            "LIVE402_PQ_SIGNER_MAINNET_TOKEN",
            "LIVE402_PQ_CONFIRM_TATUM_API_KEY",
            "LIVE402_PQ_CONFIRM_INDEXER_TOKEN",
        ):
            self.assertNotIn(key, html)
        self.assertNotIn(_MAINNET_TOKEN_NAME, html)

    def test_testnet_confirmed_under_mainnet_identity_stays_testnet_labeled(self):
        self._restart_with_mainnet_identity()
        store.append(b"one")
        _confirm(
            1,
            store.root(1),
            _TX_A,
            55,
            1_700_000_600,
            origin=ORIGIN,
            network="testnet",
            genesis_id=algo_anchor.TESTNET_GENESIS_ID,
        )
        html = self._html()
        self.assertIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertIn("(Pera Explorer, TestNet)", html)
        self.assertNotIn(algo_anchor.MAINNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertNotRegex(html, r"testnet\.explorer\.perawallet\.app/tx/%s[^<]*MainNet" % _TX_A)
        self.assertNotIn("View raw MainNet transaction JSON", html)
        self.assertIn("View raw TestNet transaction JSON", html)

    def test_authorized_never_renders_as_confirmed_with_mainnet_secrets(self):
        self._restart_with_mainnet_identity()
        store.append(b"one")
        store.save_authorized_checkpoint(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=store.root(1),
            checkpoint="note",
            request_id="rid",
            signed=b"signed-blob",
            at=1,
        )
        html = self._html()
        self.assertIn("AUTHORIZED · awaiting MainNet confirmation", html)
        self.assertIn("Awaiting first confirmed MainNet checkpoint.", html)
        self.assertNotIn('class="confirm-card"', html)
        self.assertNotIn(algo_anchor.MAINNET_EXPLORER_TX_URL, html)
        self.assertNotIn(algo_anchor.TESTNET_EXPLORER_TX_URL, html)
        home = self._html("/")
        self.assertNotIn("Latest confirmed Tree", home)

    def test_submitted_never_renders_as_confirmed(self):
        store.append(b"one")
        store.save_authorized_checkpoint(
            tree_size=1,
            origin=ORIGIN,
            root=store.root(1),
            checkpoint="note",
            request_id="rid",
            signed=b"signed-blob",
            at=1,
            submitted=True,
            txid=_TX_A,
        )
        html = self._html()
        self.assertIn("SUBMITTED · awaiting TestNet confirmation", html)
        self.assertIn("Awaiting first confirmed TestNet checkpoint.", html)
        self.assertNotIn('class="confirm-card"', html)
        self.assertNotIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertNotIn(algo_anchor.MAINNET_EXPLORER_TX_URL + _TX_A, html)
        home = self._html("/")
        self.assertNotIn("Latest confirmed Tree", home)

    def test_public_copy_separates_falcon_anchor_from_seller_payments(self):
        html = self._html()
        self.assertIn("native Falcon-1024 post-quantum authorization", html)
        self.assertIn("The Algorand transaction authorizes a checkpoint.", html)
        self.assertIn("It is not a merchant payment.", html)
        self.assertIn(
            "This post-quantum authorization protects the checkpoint transaction.",
            html,
        )
        self.assertIn(
            "It does not make Base or Solana merchant payments post-quantum secure.",
            html,
        )
        self.assertNotIn("\N{EM DASH}", html)


if __name__ == "__main__":
    unittest.main()
