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
        self.assertIn("Verify the transparency log", html)
        self.assertIn("PQ Trust is 402Signal's append-only transparency layer", html)
        self.assertNotIn("See the check-first flow on the", html)
        self.assertNotIn('class="signal-flow"', html)
        self.assertIn("It is not a merchant payment.", html)
        self.assertIn("native Falcon-1024 post-quantum authorization", html)
        self.assertIn("does not make Base or Solana merchant payments post-quantum secure", html)
        self.assertNotIn("PQ-safe", html)
        self.assertIn("Later rewriting inconsistent with published checkpoints becomes detectable.", html)
        self.assertIn("Awaiting first confirmed TestNet checkpoint.", html)
        self.assertNotIn("id=\"pq-testnet\"", html)
        self.assertNotIn(_LIVE_TX, html)
        self.assertNotIn("66862187", html)
        self.assertNotIn("testnet.explorer.perawallet.app/tx/", html)
        self.assertIn("LOG SIZE", html)
        self.assertIn("ANCHORS", html)
        self.assertIn("STATUS", html)
        self.assertIn("Not anchored yet", html)
        self.assertNotIn("AUTHORIZATION", html)
        self.assertNotIn("SINCE ANCHOR", html)
        self.assertIn("Falcon-1024 · f1", html)
        self.assertIn("<title>402Signal transparency log</title>", html)
        self.assertNotIn("\N{EM DASH}", html)
        self.assertIn("canonical", html)
        self.assertIn("https://402signal.com/transparency", html)
        self.assertIn("Algorand MainNet", html)
        self.assertIn("Algorand MainNet log · awaiting first confirmed checkpoint", html)
        self.assertIn("Production log identity is Algorand MainNet", html)
        self.assertNotIn("Signed checkpoints are periodically anchored to Algorand MainNet", html)
        self.assertIn("Historical TestNet archive", html)
        self.assertNotIn("quantum-proof", html.lower())
        self.assertNotIn("raw on-chain note", html.lower())
        for marker in _SECRETS:
            self.assertNotIn(marker, html)

    def test_homepage_omits_pq_card_without_confirmed(self):
        html, _hdrs = self._html("/")
        self.assertIn('id="pq-log"', html)
        self.assertIn("Verifiable routing history", html)
        self.assertIn("PQ Trust is 402Signal's append-only transparency layer", html)
        self.assertIn('class="pq-trust"', html)
        self.assertNotIn("pq-testnet", html)
        self.assertNotIn("Latest confirmed Tree", html)
        self.assertNotIn("Trust the history, too.", html)
        self.assertNotIn("View TestNet transaction", html)
        self.assertIn("Find a paid API that works right now.", html)
        self.assertIn('href="/transparency"', html)
        static = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("Trust the history, too.", static)
        self.assertNotIn("PQ transparency · TestNet", static)
        self.assertIn("Verifiable routing history", static)
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
        self.assertIn("Verify the transparency log", html)

    def test_human_pages_not_dynamic_filename(self):
        self.assertNotIn("/transparency", HUMAN_PAGES)
        self.assertIn("/contact", HUMAN_PAGES)
        self.assertIn("/transparency", HUMAN_DYNAMIC_PATHS)
        self.assertFalse((STATIC / "transparency.html").exists())

    def test_confirmed_homepage_exact_copy_and_evidence(self):
        store.append(b"one")
        _confirm(1, store.root(1), _TX_A, 66860001, 1_700_000_000)
        html, _hdrs = self._html("/")
        self.assertIn('id="pq-log"', html)
        self.assertIn("Algorand MainNet", html)
        self.assertIn("Verifiable routing history", html)
        self.assertIn("append-only Merkle log", html)
        self.assertIn("Latest confirmed Tree 1 · Round 66860001", html)
        self.assertIn(
            "Later rewriting inconsistent with published checkpoints becomes detectable.",
            html,
        )
        self.assertIn("does not make Base or Solana merchant payments post-quantum secure", html)
        self.assertNotIn("PQ-safe", html)
        self.assertIn("View verification details", html)
        self.assertNotIn("Latest checkpoint · Tree", html)
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
        self.assertIn("STATUS", html)
        self.assertIn("Confirmed", html)
        self.assertIn("TREE SIZE", html)
        self.assertIn("BLOCK", html)
        self.assertIn("TRANSACTION", html)
        self.assertIn("AUTHORIZATION", html)
        self.assertIn("Confirmed checkpoint fields", html)
        self.assertIn("<div class=\"hero-actions\">", html)
        self.assertNotIn("<p class=\"hero-actions\">", html)
        self.assertIn("could not be bound", html)
        self.assertIn(root.hex(), html)
        self.assertIn(_TX_A, html)
        self.assertIn("99", html)
        self.assertIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A, html)
        self.assertIn("View latest anchor on Pera", html)
        self.assertIn(algo_anchor.TESTNET_INDEXER_TXN_URL + _TX_A, html)
        self.assertIn("View raw TestNet transaction JSON", html)
        self.assertIn(_FALCON, html)
        self.assertIn("OBHYXCUV…34IFFIU", html)
        self.assertIn("View Falcon account on Pera", html)
        self.assertNotIn("View all anchors on Pera", html)
        self.assertIn("VERIFIED AT", html)
        self.assertIn("https://testnet.explorer.perawallet.app/address/" + _FALCON + "/", html)
        self.assertIn("Not every transaction on that account is a valid 402Signal checkpoint", html)
        self.assertIn("Canonical PQ1 note", html)
        self.assertIn("Reconstructed from the fields independently verified", html)
        self.assertIn(NOTE_FORMAT, html)
        self.assertIn("EXPECTED ORIGIN", html)
        self.assertIn("402signal.com/pq/log", html)
        self.assertNotIn("raw on-chain note", html.lower())
        self.assertIn("Caught up", html)
        self.assertNotIn("Current vs anchored", html)
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
        self.assertIn("Not anchored yet", html)
        self.assertIn("The log has entries. Awaiting first confirmed TestNet checkpoint.", html)
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
        self.assertIn("Awaiting first confirmed TestNet checkpoint.", html)
        self.assertNotIn(algo_anchor.TESTNET_EXPLORER_TX_URL, html)
        home, _ = self._html("/")
        self.assertNotIn("Latest confirmed Tree", home)

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
        self.assertNotIn("Latest anchor · Tree 1", html)
        self.assertNotIn(algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A, html)
        home, _ = self._html("/")
        self.assertNotIn("Latest confirmed Tree", home)

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
        self.assertIn("Tree size at each time 402Signal verified a confirmed TestNet anchor", html)
        self.assertIn("Caught up", html)
        self.assertNotIn("Current vs anchored", html)
        model = pq_view.page_model()
        sizes = [row["size"] for row in model["history"]]
        self.assertEqual(sizes, [3, 1])
        self.assertEqual(model["history"][0]["delta"], 2)
        self.assertEqual(model["history"][1]["delta"], 1)
        store.append(b"four")
        grown, _ = self._html()
        self.assertIn("1 newer log entries exist after the latest confirmed anchor.", grown)
        self.assertIn("1 newer entries", grown)
        self.assertNotIn("newer log entries since the latest confirmed anchor", grown)

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
        self.assertNotIn(_LIVE_TX, src)
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

    def test_confirmed_proof_binds_to_confirmed_size_not_latest(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from live402.pq import checkpoint as ckpt
        from live402.pq import receipt

        key = Ed25519PrivateKey.generate()
        receipt.configure_signer(key)
        try:
            store.append(b"one")
            note1 = ckpt.sign_checkpoint(ORIGIN, 1, store.root(1), key)
            store.save_checkpoint(1, note1)
            _confirm(1, store.root(1), _TX_A, 10, 100)
            store.append(b"two")
            note2 = ckpt.sign_checkpoint(ORIGIN, 2, store.root(2), key)
            store.save_checkpoint(2, note2)
            html, _ = self._html()
            self.assertIn("Latest confirmed checkpoint", html)
            self.assertIn("/pq/log/checkpoint/1", html)
            self.assertIn("View signed checkpoint for tree 1", html)
            self.assertIn("Latest signed checkpoint. It may be newer than the latest TestNet anchor.", html)
            self.assertNotIn(note2, html)
            self.assertIn(store.root(1).hex(), html)
            self.assertNotIn("\N{EM DASH}", html)
            model = pq_view.page_model()
            self.assertEqual(model["confirmed_size"], 1)
            self.assertEqual(model["current_size"], 2)
            self.assertIsNotNone(model["bound_checkpoint"])
            self.assertEqual(model["bound_checkpoint"]["size"], 1)
            self.assertEqual(model["latest_checkpoint_size"], 2)
            status, latest, _hdrs = _get(self.port, "/pq/log/checkpoint")
            self.assertEqual(status, 200)
            self.assertEqual(latest, note2)
            status, hist, _hdrs = _get(self.port, "/pq/log/checkpoint/1")
            self.assertEqual(status, 200)
            self.assertEqual(hist, note1)
            status, cur, _hdrs = _get(self.port, "/pq/log/checkpoint/2")
            self.assertEqual(status, 200)
            self.assertEqual(cur, note2)
        finally:
            receipt.configure_signer(None)

    def test_integrity_error_when_local_smaller_than_confirmed(self):
        for i in range(9):
            store.append(("leaf-%s" % i).encode())
        _confirm(10, bytes(range(32)), _TX_A, 99, 100)
        html, _ = self._html()
        self.assertNotIn("Caught up", html)
        self.assertIn("LOCAL LOG INCONSISTENT", html)
        self.assertIn(
            "The local transparency log is smaller than its latest confirmed historical checkpoint.",
            html,
        )
        self.assertNotIn("The latest log checkpoint is anchored.", html)
        home, _ = self._html("/")
        self.assertNotIn("Latest confirmed Tree", home)
        self.assertNotIn("Latest checkpoint · Tree 10", home)
        self.assertNotIn("Latest anchor · Tree 10", home)
        self.assertEqual(worker.homepage_pq_html(), "")

    def test_history_count_and_cap(self):
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
        for i in range(1, 4):
            store.append(("x%s" % i).encode())
            txid = (alphabet[i] * 52)
            _confirm(i, store.root(i), txid, 10 + i, 100 + i)
        self.assertEqual(store.confirmed_anchor_count(), 3)
        self.assertEqual(len(store.list_confirmed_anchors(limit=2)), 2)
        self.assertEqual(len(store.list_confirmed_anchors(limit=250)), 3)
        html, _ = self._html()
        self.assertIn("TOTAL CONFIRMED ANCHORS 3", html)
        self.assertNotIn("Showing latest 250 anchors.", html)

    def test_history_truncation_notice(self):
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
        for i in range(1, 252):
            txid = alphabet[i % 32] * 51 + alphabet[(i // 32) % 32]
            _confirm(i, bytes([(i % 256)] * 32), txid, i, i)
        self.assertEqual(store.confirmed_anchor_count(), 251)
        self.assertEqual(len(store.list_confirmed_anchors()), 250)
        html, _ = self._html()
        self.assertIn("TOTAL CONFIRMED ANCHORS 251", html)
        self.assertIn("Showing latest 250 anchors.", html)
        self.assertIn("leaves 2–2", html)
        self.assertNotIn("leaves 1–2", html)
        self.assertNotIn("leaves 1–251", html)
        first_txid = alphabet[1 % 32] * 51 + alphabet[(1 // 32) % 32]
        self.assertNotIn(first_txid, html)
        model = pq_view.page_model()
        sizes = [row["size"] for row in model["history"]]
        self.assertEqual(max(sizes), 251)
        self.assertEqual(min(sizes), 2)
        self.assertEqual(len(model["history"]), 250)
        oldest = [row for row in model["history"] if row["size"] == 2][0]
        self.assertEqual(oldest["delta"], 1)
        self.assertEqual(oldest["span"], "leaves 2–2")


class BoundCheckpointFailClosedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        store.append(b"one")
        self.root = store.root(1)
        self.conf = {
            "size": 1,
            "origin": ORIGIN,
            "root": self.root.hex(),
        }

    def tearDown(self):
        from live402.pq import receipt

        receipt.configure_signer(None)
        store.reset()
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        os.environ.pop("LIVE402_PQ_LOG_VKEY", None)
        self.tmp.cleanup()

    def _sign(self, key):
        from live402.pq import checkpoint as ckpt
        from live402.pq import receipt

        receipt.configure_signer(key)
        note = ckpt.sign_checkpoint(ORIGIN, 1, self.root, key)
        store.save_checkpoint(1, note)
        return note

    def test_a_missing_vkey_fails(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from live402.pq import receipt

        self._sign(Ed25519PrivateKey.generate())
        receipt.configure_signer(None)
        os.environ.pop("LIVE402_PQ_LOG_VKEY", None)
        self.assertEqual(pq_view.public_vkey(), "")
        self.assertIsNone(pq_view.bound_checkpoint(self.conf))

    def test_b_tampered_sig_fails(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        note = self._sign(Ed25519PrivateKey.generate())
        flipped = note[:-2] + ("A" if note[-2] != "A" else "B") + note[-1]
        store.save_checkpoint(1, flipped)
        self.assertIsNone(pq_view.bound_checkpoint(self.conf))

    def test_c_wrong_key_fails(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from live402.pq import receipt

        self._sign(Ed25519PrivateKey.generate())
        receipt.configure_signer(Ed25519PrivateKey.generate())
        self.assertIsNone(pq_view.bound_checkpoint(self.conf))

    def test_d_correct_passes(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self._sign(Ed25519PrivateKey.generate())
        bound = pq_view.bound_checkpoint(self.conf)
        self.assertIsNotNone(bound)
        self.assertEqual(bound["size"], 1)
        self.assertEqual(bound["origin"], ORIGIN)
        self.assertEqual(bound["root_hex"], self.root.hex())
        self.assertEqual(bound["href"], "/pq/log/checkpoint/1")


class TransparencyPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(self.tmp.name, "pq-log.sqlite")
        store.reset()
        worker.clear_queue()
        self.httpd, self.port = _serve()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        worker.clear_queue()
        store.reset()
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        self.tmp.cleanup()

    def test_transparency_has_no_customer_activity_ui(self):
        status, html, _hdrs = _get(self.port, "/transparency")
        self.assertEqual(status, 200)
        self.assertIn("Public transparency commitments do not expose raw needs, wallets, payment signatures, or seller response bodies.", html)
        self.assertIn("What is published?", html)
        self.assertIn("This page publishes 402Signal infrastructure commitments", html)
        low = html.lower()
        self.assertNotIn("is anonymous", low)
        self.assertNotIn("is unlinkable", low)
        self.assertNotIn("is fully private", low)
        self.assertIn("not a claim of anonymous, unlinkable, or fully private", low)
        self.assertIn("it does not publish agent needs, wallets, payment signatures, seller response bodies, raw requests, or payment credentials.", low)
        self.assertNotIn("doesn’t reveal", html)
        self.assertNotIn("doesn't reveal", html)
        self.assertNotIn("does not reveal", low)
        self.assertNotIn("<form", html)
        self.assertNotIn('id="need"', html)
        self.assertNotIn('name="need"', html)
        self.assertNotIn('id="prompt"', html)
        self.assertNotIn("PAYMENT-SIGNATURE", html)
        self.assertNotIn("X-PAYMENT", html)
        self.assertNotIn("payer_address", html)
        self.assertNotIn("payment_signature", html)
        self.assertNotIn("seller_body", html)
        self.assertNotIn("customer-search", html)
        self.assertNotIn("wallet feed", html.lower())
        self.assertNotIn("customer feed", html.lower())


class TransparencyHelperTests(unittest.TestCase):
    def test_abbreviate_falcon_is_base32_not_hex(self):
        shown = pq_view.abbreviate_falcon(_FALCON)
        self.assertEqual(shown, "OBHYXCUV…34IFFIU")
        self.assertNotIn("hex", shown.lower())

    def test_pera_and_indexer_urls(self):
        self.assertEqual(
            pq_view.pera_tx_url(_TX_A, "testnet"),
            algo_anchor.TESTNET_EXPLORER_TX_URL + _TX_A,
        )
        self.assertEqual(pq_view.pera_tx_url(_TX_A), "")
        self.assertEqual(pq_view.pera_tx_url("nope", "testnet"), "")
        self.assertEqual(pq_view.pera_tx_url("nope"), "")
        self.assertEqual(
            pq_view.indexer_tx_url(_TX_A, "testnet"),
            algo_anchor.TESTNET_INDEXER_TXN_URL + _TX_A,
        )
        self.assertEqual(pq_view.indexer_tx_url(_TX_A), "")
        self.assertEqual(
            pq_view.pera_tx_url(_TX_A, "mainnet"),
            algo_anchor.MAINNET_EXPLORER_TX_URL + _TX_A,
        )
        self.assertNotIn(
            "testnet.explorer.perawallet.app",
            pq_view.pera_tx_url(_TX_A, "mainnet"),
        )


if __name__ == "__main__":
    unittest.main()
