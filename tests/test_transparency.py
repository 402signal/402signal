"""GET /transparency and homepage PQ card. Presentation / read-model only."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("LIVE402_FIXTURE", "1")

from live402.pq import ORIGIN, NOTE_FORMAT, algo_anchor, store, worker
from live402.pq import transparency as pq_view
from live402.server import HUMAN_DYNAMIC_PATHS, HUMAN_PAGES, Handler

STATIC = Path(__file__).resolve().parent.parent / "live402" / "static"
_TX_A = "B" * 52
_TX_B = "C" * 52
_LIVE_TX = "V2HBS4MPRE5SCT62VLVPTGQYANBQAEOMNDYSSVAUTBFRX4PQDE4Q"
_FALCON = "OBHYXCUVOLSTZVBN5JUFIYBD4X4ZFIAFZMWMU2P45VBYGWT26MV34IFFIU"
_ROOT_A = bytes(range(32))
_ROOT_B = bytes(range(32, 64))
_SECRETS = (
    "LIVE402_PQ_FALCON_SK",
    "LIVE402_PQ_LOG_SK",
    "LIVE402_PQ_SIGNER_TOKEN",
    "HMAC",
    "pq-anchor/1",
)


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


def _get(port, path, method="GET"):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path)
    res = conn.getresponse()
    raw = res.read()
    hdrs = {k.lower(): v for k, v in res.getheaders()}
    conn.close()
    return res.status, raw.decode("utf-8"), hdrs


def _confirm(size, root, txid, rnd, at, origin=ORIGIN):
    store.save_confirmed_checkpoint(
        tree_size=size,
        origin=origin,
        root=root,
        txid=txid,
        confirmed_round=rnd,
        at=at,
    )


class TransparencyPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        os.environ["LIVE402_PQ_FALCON_ADDRESS"] = _FALCON
        store.reset()
        worker.clear_queue()
        self.httpd, self.port = _serve()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        worker.clear_queue()
        store.reset()
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        os.environ.pop("LIVE402_PQ_FALCON_ADDRESS", None)
        self.tmp.cleanup()

    def _html(self, path="/transparency"):
        status, html, hdrs = _get(self.port, path)
        self.assertEqual(status, 200, path)
        return html, hdrs

    def test_get_transparency_200_empty(self):
        html, hdrs = self._html()
        self.assertIn("text/html", hdrs.get("content-type", ""))
        csp = hdrs.get("content-security-policy") or ""
        self.assertIn("script-src 'self'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertIn("Verify 402Signal", html)
        self.assertIn("TestNet anchoring has not yet produced a confirmed checkpoint.", html)
        self.assertNotIn("id=\"pq-testnet\"", html)
        self.assertNotIn(_LIVE_TX, html)
        self.assertNotIn("66862187", html)
        self.assertNotIn("testnet.explorer.perawallet.app/tx/", html)
        self.assertIn("LOG SIZE", html)
        self.assertIn("ANCHORS CONFIRMED", html)
        self.assertNotIn("AUTHORIZATION", html)
        self.assertIn("Falcon-1024 · f1", html)
        self.assertIn("<title>402Signal Transparency — Verify the routing history</title>", html)
        self.assertIn("canonical", html)
        self.assertIn("https://402signal.com/transparency", html)
        self.assertNotIn("MainNet", html)
        self.assertNotIn("quantum-proof", html.lower())
        self.assertNotIn("raw on-chain note", html.lower())
        for marker in _SECRETS:
            self.assertNotIn(marker, html)

    def test_homepage_omits_pq_card_without_confirmed(self):
        html, _hdrs = self._html("/")
        self.assertNotIn('id="pq-testnet"', html)
        self.assertNotIn("PQ transparency · TestNet", html)
        self.assertNotIn("Trust the history, too.", html)
        self.assertNotIn("View TestNet transaction", html)
        self.assertIn("Find a paid API that works right now.", html)
        self.assertIn('href="/transparency"', html)
        static = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("Trust the history, too.", static)
        self.assertNotIn("PQ transparency · TestNet", static)
        self.assertNotIn(_LIVE_TX, static)
        self.assertEqual(worker.homepage_pq_html(), "")

    def test_head_transparency(self):
        status, body, hdrs = _get(self.port, "/transparency", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, "")
        self.assertGreater(int(hdrs.get("content-length") or 0), 0)
        status_html, body_html, _hdrs = _get(self.port, "/transparency.html", method="HEAD")
        self.assertEqual(status_html, 200)
        self.assertEqual(body_html, "")
        html, _ = self._html("/transparency.html")
        self.assertIn("Verify 402Signal", html)

    def test_human_pages_not_dynamic_filename(self):
        self.assertNotIn("/transparency", HUMAN_PAGES)
        self.assertIn("/transparency", HUMAN_DYNAMIC_PATHS)
        self.assertFalse((STATIC / "transparency.html").exists())

    def test_confirmed_homepage_exact_copy_and_evidence(self):
        store.append(b"one")
        _confirm(1, store.root(1), _TX_A, 66860001, 1_700_000_000)
        html, _hdrs = self._html("/")
        self.assertIn('id="pq-testnet"', html)
        self.assertIn("PQ transparency · TestNet", html)
        self.assertIn("Trust the history, too.", html)
        self.assertIn(
            "As agents start spending money on your behalf, there should be a record of what they relied on.",
            html,
        )
        self.assertIn("Latest checkpoint · Tree 1 · Block 66860001 · Confirmed", html)
        self.assertIn("View transparency log", html)
        self.assertIn('href="/transparency"', html)
        self.assertNotIn("View TestNet transaction", html)
        self.assertNotIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertEqual(html.count("<h1"), 1)
        self.assertNotIn(_LIVE_TX, html)

    def test_confirmed_page_dynamic_fields_and_pera(self):
        store.append(b"one")
        root = store.root(1)
        _confirm(1, root, _TX_A, 99, 1_700_000_100)
        html, _hdrs = self._html()
        self.assertIn("Status", html)
        self.assertIn("Confirmed", html)
        self.assertIn(root.hex(), html)
        self.assertIn(_TX_A, html)
        self.assertIn("99", html)
        self.assertIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertIn("View latest anchor on Pera", html)
        self.assertIn(algo_anchor.TESTNET_INDEXER_TXN_URL + _TX_A, html)
        self.assertIn("View raw TestNet transaction JSON", html)
        self.assertIn(_FALCON, html)
        self.assertIn("OBHYXCUV…34IFFIU", html)
        self.assertIn("View all anchors on Pera", html)
        self.assertIn("https://testnet.explorer.perawallet.app/address/" + _FALCON + "/", html)
        self.assertIn("Not every transaction on that account is a valid 402Signal checkpoint", html)
        self.assertIn("Canonical PQ1 note", html)
        self.assertIn("Reconstructed from the fields independently verified", html)
        self.assertIn(NOTE_FORMAT, html)
        self.assertIn("EXPECTED ORIGIN", html)
        self.assertIn("402signal.com/pq/log", html)
        self.assertNotIn("raw on-chain note", html.lower())
        self.assertIn("Caught up", html)
        self.assertIn("The latest log checkpoint is anchored.", html)
        self.assertIn("TOTAL CONFIRMED ANCHORS 1", html)
        self.assertNotIn("growth-chart", html)
        self.assertNotIn("Logged event types", html)
        self.assertIn('<time datetime="', html)
        self.assertIn("Falcon-1024 (f1)", html)
        self.assertIn("Algorand base32", html)

    def test_unanchored_growth_and_log_exists(self):
        store.append(b"one")
        store.append(b"two")
        html, _hdrs = self._html()
        self.assertIn("Log exists · no confirmed checkpoint yet", html)
        self.assertIn("The log has entries, and TestNet anchoring has not yet produced a confirmed checkpoint.", html)
        self.assertNotIn("insecure", html.lower())
        self.assertNotIn("unverified", html.lower())

    def test_authorized_not_confirmed(self):
        store.append(b"one")
        store.save_authorized_checkpoint(
            tree_size=1,
            origin=ORIGIN,
            root=store.root(1),
            checkpoint="note",
            request_id="rid",
            signed=b"signed-blob",
            at=1,
        )
        html, _hdrs = self._html()
        self.assertIn("AUTHORIZED · awaiting TestNet confirmation", html)
        self.assertIn("TestNet anchoring has not yet produced a confirmed checkpoint.", html)
        self.assertNotIn(algo_anchor.TESTNET_EXPLORER_TX_URL, html)
        home, _ = self._html("/")
        self.assertNotIn('id="pq-testnet"', home)

    def test_submitted_not_confirmed(self):
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
        html, _hdrs = self._html()
        self.assertIn("SUBMITTED · awaiting TestNet confirmation", html)
        self.assertNotIn("Latest checkpoint · Tree 1", html)
        self.assertNotIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A, html)
        home, _ = self._html("/")
        self.assertNotIn('id="pq-testnet"', home)

    def test_one_anchor_history_delta_and_no_chart(self):
        store.append(b"one")
        _confirm(1, store.root(1), _TX_A, 10, 100)
        html, _hdrs = self._html()
        self.assertIn("TOTAL CONFIRMED ANCHORS 1", html)
        self.assertIn("Δ LEAVES 1", html)
        self.assertIn("leaves 1–1", html)
        self.assertIn("Δ LEAVES", html)
        self.assertNotIn("growth-chart", html)
        self.assertNotIn("demo", html.lower())

    def test_multi_anchor_history_delta_and_chart(self):
        store.append(b"one")
        store.append(b"two")
        store.append(b"three")
        _confirm(1, store.root(1), _TX_A, 10, 100)
        _confirm(3, store.root(3), _TX_B, 20, 200)
        html, _hdrs = self._html()
        self.assertIn("TOTAL CONFIRMED ANCHORS 2", html)
        self.assertIn(_TX_A, html)
        self.assertIn(_TX_B, html)
        self.assertIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_B, html)
        self.assertIn("growth-chart", html)
        self.assertIn("joins those observations", html)
        self.assertIn("2 newer log entries exist after the latest confirmed anchor.", html)
        self.assertIn("2 newer log entries since the latest confirmed anchor", html)
        model = pq_view.page_model()
        sizes = [row["size"] for row in model["history"]]
        self.assertEqual(sizes, [3, 1])
        self.assertEqual(model["history"][0]["delta"], 2)
        self.assertEqual(model["history"][1]["delta"], 1)

    def test_pq1_decode_and_origin_hash(self):
        store.append(b"one")
        root = store.root(1)
        note = algo_anchor.encode_note(ORIGIN, 1, root)
        decoded = pq_view.decode_pq1_note(note, ORIGIN)
        self.assertIsNotNone(decoded)
        self.assertTrue(decoded["origin_hash_matches"])
        self.assertEqual(decoded["format"], NOTE_FORMAT)
        _confirm(1, root, _TX_A, 11, 111)
        html, _hdrs = self._html()
        self.assertIn(decoded["origin_hash_hex"], html)
        self.assertIn("matches note origin-hash bytes", html)
        self.assertIn("Canonical PQ1 note · reconstructed", html)

    def test_malformed_pq1_fail_closed(self):
        self.assertIsNone(pq_view.decode_pq1_note(b"not-a-note", ORIGIN))
        garbage = b"\xff" * 84
        self.assertIsNone(pq_view.decode_pq1_note(garbage, ORIGIN))
        html = pq_view.page_html()
        self.assertNotIn(garbage.decode("latin-1"), html)

    def test_no_hardcoded_live_values_in_templates(self):
        html, _ = self._html()
        self.assertNotIn(_LIVE_TX, html)
        self.assertNotIn("66862187", html)
        src = (Path(__file__).resolve().parent.parent / "live402" / "pq" / "transparency.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("testnet.explorer.perawallet.app/tx/" + _LIVE_TX, src)
        home = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertNotIn(_LIVE_TX, home)
        self.assertNotIn("66862187", home)

    def test_homepage_cta_and_old_pera_gone(self):
        store.append(b"one")
        _confirm(1, store.root(1), _TX_A, 5, 5)
        html, _ = self._html("/")
        self.assertIn('href="/transparency"', html)
        self.assertNotIn("View TestNet transaction", html)
        static = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("View TestNet transaction", static)
        self.assertNotIn("perawallet", static)


class TransparencyHelperTests(unittest.TestCase):
    def test_abbreviate_falcon_is_base32_not_hex(self):
        shown = pq_view.abbreviate_falcon(_FALCON)
        self.assertEqual(shown, "OBHYXCUV…34IFFIU")
        self.assertNotIn("hex", shown.lower())

    def test_pera_and_indexer_urls(self):
        self.assertEqual(pq_view.pera_tx_url(_TX_A), algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A)
        self.assertEqual(pq_view.pera_tx_url("nope"), "")
        self.assertEqual(
            pq_view.indexer_tx_url(_TX_A),
            algo_anchor.TESTNET_INDEXER_TXN_URL + _TX_A,
        )


if __name__ == "__main__":
    unittest.main()
