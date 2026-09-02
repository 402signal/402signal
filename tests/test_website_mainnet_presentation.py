"""Website/transparency MainNet-only presentation and explorer isolation.

COPY_EXPLORER_ISOLATION is the named suite for this closeout.
Public production copy is MainNet-only with an honest awaiting line.
Explorer URLs come from independently confirmed anchor state.
MainNet evidence never links to a TestNet explorer. Unverified MainNet
explorer URLs are suppressed. AUTHORIZED/SUBMITTED never render as CONFIRMED.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402 import payment
from live402.pq import ORIGIN, ORIGIN_MAINNET, algo_anchor, store, worker
from live402.pq import transparency as pq_view
from live402.server import Handler
from tests.pq_test_env import clear_pq_env

STATIC = Path(__file__).resolve().parent.parent / "live402" / "static"
_TX_A = "B" * 52
_TX_B = "C" * 52
_FALCON_TESTNET = "OBHYXCUVOLSTZVBN5JUFIYBD4X4ZFIAFZMWMU2P45VBYGWT26MV34IFFIU"
_MAINNET_TOKEN_NAME = "named-not-valued"
_UNKNOWN_ORIGIN = "example.invalid/pq/log/unknown"
_BANNED_CLAIMS = (
    "PQ-payments",
    "PQ payments",
    "quantum-secure seller",
    "quantum-secure seller payments",
    "Algorand-verified-API",
    "Algorand-verified API",
    "immutable-truth",
    "immutable truth",
    "post-quantum Base",
    "post-quantum Solana",
)


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


def _assert_no_banned_copy(test, html: str, where: str) -> None:
    test.assertNotIn("\N{EM DASH}", html, where)
    low = html.lower()
    for phrase in _BANNED_CLAIMS:
        test.assertNotIn(phrase.lower(), low, "%s: %s" % (where, phrase))
    test.assertNotIn("currently testnet", low, where)
    test.assertNotIn("pq-testnet", html, where)


class COPY_EXPLORER_ISOLATION(unittest.TestCase):
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

    def test_production_pages_are_mainnet_pq_trust_and_awaiting(self):
        home = self._html("/")
        how = self._html("/how")
        catalog = self._html("/catalog")
        trans = self._html("/transparency")
        devs = self._html("/developers")
        for path, html in (
            ("/", home),
            ("/how", how),
            ("/catalog", catalog),
            ("/transparency", trans),
            ("/developers", devs),
        ):
            self.assertNotIn("Currently Algorand TestNet", html, path)
            self.assertNotIn("periodically anchored to Algorand TestNet", html, path)
            self.assertNotIn("periodically anchored to Algorand MainNet", html, path)
            _assert_no_banned_copy(self, html, path)
        for path, html in (("/", home), ("/transparency", trans)):
            self.assertIn("Algorand MainNet", html, path)
        self.assertIn('class="pq-trust"', home)
        self.assertIn("Algorand MainNet", home)
        self.assertIn("Awaiting anchor", home)
        self.assertIn('class="pq-chip"', home)
        self.assertIn("402Signal records routing evidence in an append-only Merkle log.", home)
        self.assertIn("402Signal records routing evidence in an append-only Merkle log.", trans)
        self.assertNotIn("Latest confirmed Tree", home)
        self.assertNotIn('class="confirm-card"', trans)
        self.assertIn("Awaiting anchor", trans)
        self.assertIn("e6b81414", trans)
        self.assertNotIn("Algorand MainNet log · awaiting first confirmed checkpoint", home)
        self.assertNotIn("Algorand MainNet log · awaiting first confirmed checkpoint", trans)
        self.assertNotIn(algo_anchor.TESTNET_EXPLORER_TX_URL, home)
        self.assertNotIn(algo_anchor.MAINNET_EXPLORER_TX_URL, home)

    def test_static_homepage_retired_live_testnet_copy(self):
        home = (STATIC / "index.html").read_text(encoding="utf-8")
        how = (STATIC / "how.html").read_text(encoding="utf-8")
        self.assertIn('class="pq-trust"', home)
        self.assertNotIn("pq-testnet", home)
        self.assertNotIn("currently TestNet", home)
        self.assertNotIn("Currently Algorand TestNet", home)
        self.assertIn("Algorand MainNet", home)
        self.assertIn("Awaiting anchor", home)
        self.assertIn("What happens during a route check", how)
        self.assertNotIn("Algorand MainNet log · awaiting first confirmed checkpoint", home)
        _assert_no_banned_copy(self, home, "index.html")
        _assert_no_banned_copy(self, how, "how.html")
        self.assertLessEqual(len(payment.CATALOG_DESCRIPTION), 500)
        self.assertIn("PQ Trust", payment.CATALOG_DESCRIPTION)
        self.assertIn("Algorand MainNet", payment.CATALOG_DESCRIPTION)
        self.assertNotIn("currently TestNet", payment.CATALOG_DESCRIPTION)

    def test_confirmed_testnet_anchor_uses_testnet_explorer_only(self):
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
        _assert_no_banned_copy(self, html, "confirmed-testnet")
        self.assertIn("The Algorand transaction authorizes a checkpoint.", html)
        self.assertIn("It is not a merchant payment.", html)
        self.assertNotIn("Confirmed checkpoints are independently anchored to Algorand MainNet", html)
        self.assertIn("Awaiting anchor", html)

    def test_confirmed_mainnet_anchor_never_links_testnet_explorer(self):
        self._restart_with_mainnet_identity()
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
        html = self._html()
        self.assertIn("Confirmed", html)
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
        home = self._html("/")
        self.assertIn("Anchored", home)
        self.assertIn('class="pq-chip is-anchored"', home)
        self.assertNotIn("Awaiting anchor", home)
        self.assertNotIn("Latest confirmed Tree", home)
        self.assertNotIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A, home)
        _assert_no_banned_copy(self, html, "confirmed-mainnet")

    def test_mainnet_origin_without_genesis_suppresses_explorer(self):
        store.append(b"one")
        _confirm(1, store.root(1), _TX_A, 12, 1_700_000_300, origin=ORIGIN_MAINNET)
        html = self._html()
        self.assertIn("Confirmed", html)
        self.assertNotIn(algo_anchor.MAINNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertNotIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertNotIn("View latest anchor on Pera", html)
        view = pq_view.confirmed_view()
        self.assertIsNotNone(view)
        self.assertEqual(view["explorer"], "")
        self.assertEqual(view["network"], "")

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

    def test_authorized_and_submitted_are_not_confirmed(self):
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
        self.assertIn("Awaiting anchor", html)
        self.assertNotIn('class="confirm-card"', html)
        self.assertNotIn("Latest confirmed Tree", html)
        self.assertNotIn(algo_anchor.MAINNET_EXPLORER_TX_URL, html)
        self.assertNotIn(algo_anchor.TESTNET_EXPLORER_TX_URL, html)
        home = self._html("/")
        self.assertNotIn("Latest confirmed Tree", home)
        self.assertIn("Awaiting anchor", home)
        self.assertNotIn('class="pq-chip is-anchored"', home)

        store.save_authorized_checkpoint(
            tree_size=1,
            origin=ORIGIN_MAINNET,
            root=store.root(1),
            checkpoint="note",
            request_id="rid",
            signed=b"signed-blob",
            at=1,
            submitted=True,
            txid=_TX_A,
        )
        submitted = self._html()
        self.assertIn("SUBMITTED · awaiting MainNet confirmation", submitted)
        self.assertIn("Awaiting anchor", submitted)
        self.assertNotIn('class="confirm-card"', submitted)
        self.assertNotIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A, submitted)
        self.assertNotIn(algo_anchor.MAINNET_EXPLORER_TX_URL + _TX_A, submitted)
        home2 = self._html("/")
        self.assertNotIn("Latest confirmed Tree", home2)
        self.assertIn("Awaiting anchor", home2)
        self.assertNotIn('class="pq-chip is-anchored"', home2)

    def test_mainnet_secrets_do_not_relabel_testnet_evidence(self):
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
        for key in (
            "LIVE402_PQ_SIGNER_MAINNET_TOKEN",
            "LIVE402_PQ_CONFIRM_TATUM_API_KEY",
            "LIVE402_PQ_CONFIRM_INDEXER_TOKEN",
        ):
            self.assertNotIn(key, html)
        self.assertNotIn(_MAINNET_TOKEN_NAME, html)

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
        self.assertEqual(
            algo_anchor.explorer_hint_label(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A),
            "TestNet",
        )
        self.assertEqual(
            algo_anchor.explorer_hint_label(algo_anchor.MAINNET_EXPLORER_TX_URL + _TX_A),
            "MainNet",
        )
        self.assertEqual(algo_anchor.explorer_hint_label("https://example.test/tx/" + _TX_A), "")


if __name__ == "__main__":
    unittest.main()
