import base64
import json
import os
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import urllib.error
import urllib.request

# Fixture mode for probe tests. Paywall tests still 402 unless LOCAL_FREE=1.
os.environ.setdefault("LIVE402_FIXTURE", "1")
os.environ.pop("LOCAL_FREE", None)

from live402.server import Handler
from live402 import facilitator, payment, probe, fixtures


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return httpd, host, port


def _json_post(port, path, payload, extra_headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    conn.request("POST", path, body=body, headers=headers)
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        data = raw.decode("utf-8")
    return res.status, data


def _get(port, path, extra_headers=None):
    status, body, _hdrs = _get_full(port, path, extra_headers)
    return status, body


def _get_full(port, path, extra_headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = dict(extra_headers or {})
    conn.request("GET", path, headers=headers)
    res = conn.getresponse()
    raw = res.read()
    hdrs = {k.lower(): v for k, v in res.getheaders()}
    conn.close()
    return res.status, raw.decode("utf-8"), hdrs


def _head(port, path, extra_headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = dict(extra_headers or {})
    conn.request("HEAD", path, headers=headers)
    res = conn.getresponse()
    raw = res.read()
    hdrs = {k.lower(): v for k, v in res.getheaders()}
    conn.close()
    return res.status, raw, hdrs


class PaywallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("LOCAL_FREE", None)
        cls.httpd, cls.host, cls.port = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_unpaid_route_returns_402(self):
        status, body = _json_post(
            self.port, "/route", {"need": "erc20 token balance"}
        )
        self.assertEqual(status, 402)
        self.assertEqual(body["network"], "base")
        self.assertEqual(body["asset"], "USDC")
        self.assertEqual(body["amount"], "$0.01")
        self.assertEqual(body["payTo"], payment.DEFAULT_PAYTO)
        networks = [a.get("network") for a in body.get("accepts") or []]
        self.assertIn(payment.BASE_CAIP2, networks)
        self.assertTrue(any(str(n).startswith("algorand:") for n in networks))
        self.assertTrue(any(str(n).startswith("solana:") for n in networks))
        base = next(a for a in body["accepts"] if a.get("network") == payment.BASE_CAIP2)
        self.assertEqual(base["asset"], payment.USDC_BASE)
        self.assertEqual(base["currency"], payment.USDC_BASE)
        self.assertEqual(base["amount"], payment.AMOUNT_ATOMIC)
        self.assertEqual(base["extra"]["displayAmount"], payment.AMOUNT_USD)
        algo = next(a for a in body["accepts"] if str(a.get("network","")).startswith("algorand:"))
        self.assertEqual(algo["payTo"], payment.DEFAULT_PAYTO_ALGORAND)
        self.assertEqual(algo["asset"], payment.USDC_ALGORAND_ASA)
        self.assertEqual(algo["extra"]["feePayer"], payment.ALGORAND_FEE_PAYER)
        self.assertEqual(algo["extra"]["facilitator"], payment.ALGORAND_FACILITATOR)
        self.assertEqual(algo["extra"]["tag"], "x402-global-challenge")
        sp = algo["extra"].get("suggestedParams") or {}
        self.assertEqual(sp.get("genesisID"), "mainnet-v1.0")
        self.assertIn("genesisHash", sp)
        self.assertIn("firstRound", sp)
        self.assertIn("lastRound", sp)
        self.assertIn("firstValid", sp)
        self.assertIn("lastValid", sp)
        self.assertIn("minFee", sp)
        ug = algo["extra"].get("unsignedGroup") or {}
        self.assertEqual(ug.get("paymentIndex"), 1)
        self.assertEqual((ug.get("feePayerTxn") or {}).get("from"), payment.ALGORAND_FEE_PAYER)
        self.assertEqual((ug.get("paymentTxn") or {}).get("to"), payment.DEFAULT_PAYTO_ALGORAND)
        self.assertEqual((ug.get("paymentTxn") or {}).get("asset"), 31566704)
        self.assertEqual((ug.get("paymentTxn") or {}).get("amount"), 10000)
        self.assertEqual((ug.get("paymentTxn") or {}).get("note"), "x402-payment-v2")
        self.assertNotIn("txns", ug)
        self.assertNotIn("tag", base.get("extra") or {})
        sol = next(a for a in body["accepts"] if str(a.get("network","")).startswith("solana:"))
        self.assertEqual(sol["payTo"], payment.DEFAULT_PAYTO_SOLANA)
        self.assertEqual(sol["asset"], payment.USDC_SOLANA_MINT)
        self.assertEqual(sol["extra"]["feePayer"], payment.SOLANA_FEE_PAYER)
        self.assertNotIn("tag", sol.get("extra") or {})
        bazaar = (body.get("extensions") or {}).get("bazaar") or {}
        self.assertIn("info", bazaar)
        self.assertEqual(bazaar["info"]["input"]["method"], "POST")
        self.assertEqual(bazaar["info"]["input"]["type"], "http")

    def test_official_requirements_tag_algo_only(self):
        tagged = {"tag": "x402-global-challenge", "name": "USD Coin"}
        algo = payment.official_requirements({
            "network": payment.ALGORAND_MAINNET,
            "extra": {},
        })
        self.assertEqual(algo["extra"]["tag"], "x402-global-challenge")
        base = payment.official_requirements({
            "network": payment.BASE_CAIP2,
            "extra": dict(tagged),
        })
        self.assertNotIn("tag", base["extra"])
        sol = payment.official_requirements({
            "network": payment.SOLANA_MAINNET,
            "extra": dict(tagged),
        })
        self.assertNotIn("tag", sol["extra"])

    def test_official_requirements_strips_algo_helpers(self):
        algo = payment.official_requirements({
            "network": payment.ALGORAND_MAINNET,
            "extra": {
                "suggestedParams": {"firstValid": 1},
                "unsignedGroup": {"paymentIndex": 1, "txns": ["a", "b"]},
                "decimals": 6,
                "sender": "X",
                "tag": "x402-global-challenge",
            },
        })
        extra = algo["extra"]
        self.assertEqual(extra["tag"], "x402-global-challenge")
        self.assertEqual(extra["facilitator"], payment.ALGORAND_FACILITATOR)
        self.assertEqual(extra["feePayer"], payment.ALGORAND_FEE_PAYER)
        self.assertNotIn("suggestedParams", extra)
        self.assertNotIn("unsignedGroup", extra)
        self.assertNotIn("decimals", extra)
        self.assertNotIn("sender", extra)

    def test_unpaid_algorand_sender_unsigned_group(self):
        sender = payment.DEFAULT_PAYTO_ALGORAND
        status, body = _json_post(
            self.port,
            "/route",
            {"need": "weather"},
            extra_headers={"Algorand-Sender": sender},
        )
        self.assertEqual(status, 402)
        algo = next(a for a in body["accepts"] if str(a.get("network", "")).startswith("algorand:"))
        ug = algo["extra"]["unsignedGroup"]
        self.assertEqual(ug["paymentIndex"], 1)
        self.assertEqual(ug["sender"], sender)
        self.assertEqual(len(ug["txns"]), 2)
        fee_raw = base64.b64decode(ug["txns"][0])
        pay_raw = base64.b64decode(ug["txns"][1])
        self.assertIn(b"x402-fee-payer", fee_raw)
        self.assertIn(b"x402-payment-v2", pay_raw)
        self.assertIn(b"pay", fee_raw)
        self.assertIn(b"axfer", pay_raw)

    def test_algo_unsigned_group_encoder(self):
        from live402 import algo_tx, algod
        params = algod.suggested_params()
        sender = payment.DEFAULT_PAYTO_ALGORAND
        group, index = algo_tx.build_unsigned_group(
            sender,
            payment.ALGORAND_FEE_PAYER,
            payment.DEFAULT_PAYTO_ALGORAND,
            10000,
            31566704,
            params,
        )
        self.assertEqual(index, 1)
        self.assertEqual(len(group), 2)
        fee_raw = base64.b64decode(group[0])
        pay_raw = base64.b64decode(group[1])
        self.assertIn(b"x402-fee-payer", fee_raw)
        self.assertIn(b"x402-payment-v2", pay_raw)
        self.assertIn(b"pay", fee_raw)
        self.assertIn(b"axfer", pay_raw)
        group2, _ = algo_tx.build_unsigned_group(
            sender,
            payment.ALGORAND_FEE_PAYER,
            payment.DEFAULT_PAYTO_ALGORAND,
            10000,
            31566704,
            params,
        )
        self.assertEqual(group, group2)

    def test_payto_from_env(self):

        os.environ["PAYTO_ADDRESS"] = "0x1111111111111111111111111111111111111111"
        try:
            status, body = _json_post(self.port, "/route", {"need": "weather"})
            self.assertEqual(status, 402)
            self.assertEqual(body["payTo"], "0x1111111111111111111111111111111111111111")
        finally:
            os.environ.pop("PAYTO_ADDRESS", None)

    def test_health_is_200(self):
        status, raw = _get(self.port, "/health")
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertEqual(body, {"ok": True})

    def test_unpaid_does_not_probe(self):
        with patch("live402.probe.probe_url") as mock_url, patch(
            "live402.probe.route_need"
        ) as mock_need:
            status, body = _json_post(
                self.port, "/route", {"need": "erc20 token balance"}
            )
            self.assertEqual(status, 402)
            self.assertIn("accepts", body)
            self.assertEqual(len(body.get("accepts") or []), 3)
            self.assertIn("bazaar", (body.get("extensions") or {}))
            mock_url.assert_not_called()
            mock_need.assert_not_called()

    def test_homepage(self):
        status, html = _get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertIn("402Signal", html)
        self.assertIn("Check before your agent pays.", html)
        self.assertIn("Catalog said X. We observed Y.", html)
        self.assertIn("Catalog claims and 402Signal observations are separate fields.", html)
        self.assertIn("Index is filling. Preview has no hits yet. Pulse counts are 0. Probe live still 402s /route.", html)
        self.assertIn("Facilitator reachability, not seller health.", html)
        self.assertIn("402Signal recommends the route. Your agent keeps the wallet.", html)
        self.assertIn("Best capability match in the current index.", html)
        self.assertIn("Probe live ($0.01)", html)
        self.assertIn('id="preview-btn"', html)
        self.assertIn('id="probe-btn"', html)
        self.assertIn("web search", html)
        self.assertNotIn("Also in this ship", html)
        self.assertNotIn("<h2>Now</h2>", html)
        self.assertNotIn("What it is", html)
        self.assertNotIn("healthy", html)
        self.assertNotIn("Executable Now Rate", html)
        self.assertNotIn("7d reliability", html)
        self.assertNotIn("cheapest", html)
        self.assertNotIn("most_reliable", html)
        self.assertIn("prefer_network", html)
        self.assertIn("miss_reason", html)
        self.assertIn("How it works", html)
        self.assertNotIn("What /route is", html)
        self.assertNotIn("For agents", html)
        self.assertNotIn(">Pay on Base<", html)
        self.assertNotIn("<h2>Next</h2>", html)
        self.assertNotIn("Try one of these", html)
        self.assertNotIn('id="route-samples"', html)
        self.assertNotIn("Listed right now", html)
        self.assertNotIn("What's listed", html)
        self.assertIn("Pulse", html)
        self.assertIn('id="pulse-home"', html)
        self.assertIn("Loading catalog counts", html)
        self.assertIn("How you'd use this", html)
        self.assertIn('href="#use"', html)
        self.assertIn('href="#overview"', html)
        self.assertIn('id="panel-use"', html)
        self.assertIn('id="panel-overview"', html)
        use_html = html.split('id="panel-use"', 1)[1]
        self.assertIn("prefer_network", use_html)
        self.assertNotIn("x402-global-challenge", use_html)
        self.assertNotIn("extra.tag", use_html)
        self.assertNotIn("href=\"/dashboard\"", html)
        self.assertIn("@402Signal", html)
        self.assertIn("Listed on", html)
        self.assertIn("https://glama.ai/mcp/servers/402signal/402signal", html)
        self.assertIn("https://smithery.ai/servers/live402/signal", html)
        self.assertIn("https://agentic.market/services/402signal-com", html)
        self.assertIn("https://facilitator.goplausible.xyz/dashboard/merchants/56466a9400d70f08", html)
        self.assertIn("https://www.x402scan.com/recipient/0xb18fc2275f36dae99eb215caeff03b431f887d16", html)
        self.assertNotIn("dashboard/resources/UE9TVDpodHRwczovLzQwMnNpZ25hbC5jb20vcm91dGU", html)
        self.assertNotIn("x402 Global Challenge", html)
        self.assertNotIn("24klabs.ai/listing/402signal", html)
        self.assertNotIn("api.cdp.coinbase.com", html)
        self.assertIn("$0.01", html)
        self.assertNotIn("Find a live URL", html)
        self.assertIn("GET /preview", html)
        self.assertIn("POST /route", html)
        self.assertIn("<code>miss_reason</code>", html)
        self.assertIn("penny POST /route", html)
        self.assertIn("What do you need?", html)
        self.assertIn("402signal@gmail.com", html)
        self.assertIn("https://github.com/402signal/402signal", html)
        self.assertIn("PAYMENT-SIGNATURE", html)
        self.assertNotIn("Show technical details", html)
        self.assertNotIn("Fail-closed x402 routing", html)
        self.assertNotIn("Catalogs go stale", html)
        self.assertNotIn("We are a router.", html)
        self.assertNotIn("IBM Plex", html)
        self.assertNotIn("#e8a317", html)
        self.assertNotIn("#0b0d0c", html)
        self.assertNotIn("\N{EM DASH}", html)
        js_path = os.path.join(os.path.dirname(__file__), "..", "live402", "static", "app.js")
        with open(js_path, encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn("This call costs $0.01 USDC", js)
        self.assertNotIn("7d reliability", js)
        self.assertNotIn("healthy", js)
        self.assertNotIn("Executable Now Rate", js)
        self.assertIn("#use", js)
        self.assertIn("panel-use", js)
        self.assertIn("panel-overview", js)
        self.assertIn('fetch("/pulse")', js)
        self.assertIn("loadPulseHome", js)
        self.assertIn("index_refreshing", js)
        self.assertIn("402signal_observed", js)
        self.assertIn("Pulse is slow right now", js)
        self.assertNotIn("success_7d", js)
        self.assertNotIn("executable_now_rate", js)
        self.assertIn("need-chips", js)
        self.assertIn('fetch("/preview', js)
        self.assertIn('fetch("/rails', js)
        self.assertIn("Preview fetch 502", js)
        self.assertIn("Best capability match in the current index.", js)
        self.assertIn("payTo_changed", js)
        self.assertIn("missing schema", js)
        self.assertIn("402 without payTo", js)
        self.assertNotIn("route-samples", js)
        self.assertIn('q.get("need")', js)
        self.assertNotIn('q.get("url")', js)
        self.assertIn("window.ethereum", js)
        self.assertIn("payBase", js)
        self.assertNotIn("payAlgo", js)
        self.assertNotIn("PeraWalletConnect", js)
        self.assertNotIn("LuteConnect", js)
        self.assertNotIn("algosdk", js)
        self.assertIn("PAYMENT-SIGNATURE", js)
        self.assertIn("eth_signTypedData_v4", js)
        self.assertIn("wallet_switchEthereumChain", js)
        self.assertIn("8453", js)
        self.assertIn("10000", js)
        self.assertIn("eip155:8453", js)
        self.assertNotIn("wrapFetchWithPayment", js)
        self.assertNotIn('samplesBox.addEventListener("click"', js)
        self.assertIn('chips.addEventListener("click"', js)
        css_path = os.path.join(os.path.dirname(__file__), "..", "live402", "static", "styles.css")
        with open(css_path, encoding="utf-8") as fh:
            css = fh.read()
        for swatch in ("#1c1c22", "#2a2a32", "#f7ebd4", "#c8ad88", "#4a453c", "#fec865", "#e49c60", "#3ecfc9", "#14141a"):
            self.assertIn(swatch, css)
        self.assertIn("a:visited", css)
        self.assertIn("a:link, a:visited", css)
        self.assertIn("var(--accent)", css)
        self.assertNotIn("#e8a317", css)
        self.assertNotIn("#0b0d0c", css)
        self.assertNotIn("#0c0c0c", css)
        self.assertNotIn("#8fbf88", css)
        self.assertNotIn("IBM Plex", css)
        self.assertIn("Georgia", css)
        self.assertIn('id="need"', html)
        self.assertIn('id="pay-base"', html)
        self.assertIn("Pay $0.01 on Base", html)
        self.assertIn('id="pay-base-hint"', html)
        self.assertNotIn('id="pay-algo"', html)
        self.assertNotIn("Pay $0.01 on Algorand", html)
        self.assertNotIn('id="pay-algo-hint"', html)
        self.assertNotIn('src="/algosdk.min.js"', html)
        self.assertNotIn('src="/pera.js"', html)
        self.assertNotIn('src="/lute.js"', html)
        self.assertNotIn("cdn.", html)
        self.assertNotIn("unpkg", html)
        self.assertNotIn("jsdelivr", html)
        self.assertIn("hidden", html)
        self.assertIn('id="preview-btn"', html)
        self.assertIn("/preview", html)
        self.assertIn("/llms.txt", html)
        self.assertIn("/rails", html)
        self.assertIn("/pulse", html)
        self.assertNotIn("For agents", html)
        self.assertIn("POST https://402signal.com/route", html)
        self.assertIn('{"need":"weather"}', html)
        self.assertIn("/openapi.json", html)
        self.assertIn("https://402signal.com/mcp", html)
        self.assertIn("/.well-known/x402.json", html)
        self.assertNotIn('data-need="erc20 token balance"', html)
        self.assertIn('id="need-chips"', html)
        self.assertNotIn('id="url"', html)
        self.assertNotIn("Pay $0.01 USDC on Base or Algorand in this browser", html)
        self.assertIn("twitter:site", html)
        self.assertIn('og:title" content="402Signal"', html)
        self.assertIn('og:url" content="https://402signal.com"', html)
        self.assertIn("og:description", html)
        self.assertNotIn("LOCAL_FREE", html)
        self.assertNotIn("do not pay for a corpse", html)
        self.assertNotIn("You pay only if it is live", html)
        self.assertNotIn("pay only if it is live", html.lower())
        self.assertNotIn("you do not pay for a corpse", html.lower())
        self.assertNotIn("welcome to", html.lower())
        self.assertNotIn("empower", html.lower())
        self.assertNotIn("seamless", html.lower())
        css_path = os.path.join(os.path.dirname(__file__), "..", "live402", "static", "styles.css")
        with open(css_path, encoding="utf-8") as fh:
            css = fh.read()
        for swatch in ("#1c1c22", "#2a2a32", "#f7ebd4", "#c8ad88", "#4a453c", "#fec865", "#e49c60", "#3ecfc9", "#14141a"):
            self.assertIn(swatch, css)
        self.assertIn("a:visited", css)
        self.assertIn("a:link, a:visited", css)
        self.assertIn("var(--accent)", css)
        self.assertNotIn("#e8a317", css)
        self.assertNotIn("#0b0d0c", css)
        self.assertNotIn("#0c0c0c", css)
        self.assertNotIn("#8fbf88", css)
        self.assertNotIn("IBM Plex", css)
        self.assertIn("Georgia", css)

    def test_get_route_html_accept_is_human_page(self):
        status, html, headers = _get_full(
            self.port,
            "/route",
            extra_headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        self.assertIn("GET", headers.get("allow", ""))
        self.assertIn("POST", headers.get("allow", ""))
        self.assertIn("OPTIONS", headers.get("allow", ""))
        self.assertIn("$0.01", html)
        self.assertIn('href="/"', html)
        self.assertIn("/pulse", html)
        self.assertIn("POST /route", html)
        self.assertIn("paid API", html)
        self.assertNotIn('{"error": "not found"}', html)
        self.assertNotIn('{"error":"not found"}', html)
        status, body = _json_post(self.port, "/route", {})
        self.assertEqual(status, 402)
        amounts = [str(a.get("amount")) for a in body.get("accepts") or []]
        self.assertEqual(amounts, ["10000", "10000", "10000"])

    def test_get_route_json_accept_is_402(self):
        status, raw, headers = _get_full(
            self.port, "/route", extra_headers={"Accept": "application/json"}
        )
        self.assertEqual(status, 402)
        self.assertIn("application/json", headers.get("content-type", ""))
        self.assertIn("GET", headers.get("allow", ""))
        self.assertIn("POST", headers.get("allow", ""))
        body = json.loads(raw)
        self.assertIn("accepts", body)
        self.assertEqual(len(body.get("accepts") or []), 3)
        amounts = [str(a.get("amount")) for a in body.get("accepts") or []]
        self.assertEqual(amounts, ["10000", "10000", "10000"])
        bazaar = (body.get("extensions") or {}).get("bazaar") or {}
        self.assertIn("info", bazaar)
        self.assertEqual(bazaar["info"]["input"]["method"], "POST")
        self.assertTrue(headers.get("payment-required"))

    def test_get_route_no_accept_is_402(self):
        status, raw, headers = _get_full(self.port, "/route")
        self.assertEqual(status, 402)
        self.assertIn("application/json", headers.get("content-type", ""))
        body = json.loads(raw)
        amounts = [str(a.get("amount")) for a in body.get("accepts") or []]
        self.assertEqual(amounts, ["10000", "10000", "10000"])
        self.assertIn("bazaar", body.get("extensions") or {})

    def test_unpaid_empty_post_still_402_with_atomic_10000(self):
        status, body = _json_post(self.port, "/route", {})
        self.assertEqual(status, 402)
        amounts = [str(a.get("amount")) for a in body.get("accepts") or []]
        self.assertEqual(amounts, ["10000", "10000", "10000"])
        self.assertEqual(body.get("amount"), "$0.01")

    def test_security_headers(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/health")
        res = conn.getresponse()
        raw = res.read()
        headers = {k.lower(): v for k, v in res.getheaders()}
        conn.close()
        self.assertEqual(res.status, 200)
        self.assertEqual(json.loads(raw.decode("utf-8")), {"ok": True})
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(headers.get("x-frame-options"), "DENY")
        self.assertEqual(headers.get("referrer-policy"), "no-referrer")
        self.assertEqual(headers.get("strict-transport-security"), "max-age=31536000")
        csp = headers.get("content-security-policy") or ""
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("cdn.", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertNotIn("*", csp.split("connect-src")[1].split(";")[0] if "connect-src" in csp else "")
        self.assertEqual(
            csp,
            "default-src 'none'; script-src 'self'; "
            "connect-src 'self'; "
            "style-src 'self'; img-src 'self' data:; base-uri 'self'; "
            "frame-ancestors 'none'",
        )
        self.assertNotIn("perawallet", csp)
        prev_status, prev_raw = _get(self.port, "/preview?need=weather")
        self.assertEqual(prev_status, 200)
        self.assertTrue(json.loads(prev_raw).get("not_probed"))
        mcp_status, mcp_raw = _get(self.port, "/mcp.json")
        self.assertEqual(mcp_status, 200)
        self.assertIn("route", [t.get("name") for t in json.loads(mcp_raw).get("tools") or []])

    def test_homepage_algorand_pay_no_mnemonic(self):
        status, html = _get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertNotIn("Pay $0.01 on Algorand", html)
        self.assertNotIn('id="pay-algo"', html)
        self.assertNotIn("mnemonic", html.lower())
        static_dir = os.path.join(os.path.dirname(__file__), "..", "live402", "static")
        for name in ("app.js", "index.html"):
            with open(os.path.join(static_dir, name), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("mnemonic", text.lower(), msg=name)
            self.assertNotIn("secretkey", text.lower().replace(" ", ""), msg=name)
            self.assertNotIn("LOCAL_FREE", text)
        py_dir = os.path.join(os.path.dirname(__file__), "..", "live402")
        for name in ("algod.py", "algo_tx.py"):
            with open(os.path.join(py_dir, name), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("mnemonic", text.lower(), msg=name)
            self.assertNotIn("secretkey", text.lower().replace(" ", ""), msg=name)
        for gone in ("pera.js", "lute.js", "algosdk.min.js"):
            self.assertFalse((Path(static_dir) / gone).exists(), gone)
        st, body, headers = _get_full(self.port, "/app.js")
        self.assertEqual(st, 200)
        self.assertNotIn("text/html", headers.get("content-type", ""))
        self.assertGreater(len(body), 100)
        js_path = os.path.join(static_dir, "app.js")
        with open(js_path, encoding="utf-8") as fh:
            js = fh.read()
        self.assertNotIn("payAlgoBtn", js)
        self.assertIn('fetch("/route"', js)
        self.assertNotIn("Algorand-Sender", js)
        self.assertNotIn("PeraWalletConnect", js)
        self.assertNotIn("perawallet-wc://", js)

    def test_csp_script_src_self_only(self):
        _st, _raw, headers = _get_full(self.port, "/")
        csp = headers.get("content-security-policy") or ""
        self.assertIn("script-src 'self'", csp)
        script_src = csp.split("script-src")[1].split(";")[0].strip()
        self.assertEqual(script_src, "'self'")
        self.assertIn("connect-src", csp)
        self.assertNotIn("https://fonts.googleapis.com", csp)
        self.assertNotIn("https://unpkg.com", csp)
        self.assertNotIn("https://cdn.", csp)
        self.assertNotIn("perawallet", csp)
        connect = csp.split("connect-src")[1].split(";")[0].strip()
        self.assertEqual(connect, "'self'")

    def test_dashboard_page(self):
        status, html = _get(self.port, "/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("Examples", html)
        self.assertIn("Lookups we can try", html)
        self.assertIn("We still probe before you pay", html)
        self.assertIn("Base", html)
        self.assertIn("Solana", html)
        self.assertIn("Algorand", html)
        self.assertIn("402signal.com/route", html)
        self.assertIn("$0.01", html)
        self.assertIn("/pulse", html)
        self.assertIn("/dashboard.js", html)
        self.assertNotIn("<script>", html.replace('<script src="/dashboard.js"></script>', ""))
        js_path = os.path.join(os.path.dirname(__file__), "..", "live402", "static", "dashboard.js")
        with open(js_path, encoding="utf-8") as fh:
            dash_js = fh.read()
        self.assertIn("setInterval", dash_js)
        self.assertIn('fetch("/pulse"', dash_js)
        self.assertIn("/?need=", html)
        self.assertNotIn("What's listed", html)
        self.assertNotIn("theme-bar", html)

    def test_pulse_json_and_ignores_caller_url(self):
        from live402 import pulse as pulse_mod
        pulse_mod.reset_cache()
        empty_idx = {
            "items": [],
            "by_rail": {"base": [], "solana": [], "algorand": []},
            "fetched_at": 0,
            "totals": {},
            "truncated": {},
            "complete": True,
            "errors": {},
        }
        with patch("live402.pulse.fixtures.fixture_mode", return_value=False), patch(
            "live402.catalog.get_index", return_value=empty_idx
        ), patch("urllib.request.urlopen") as urlopen:
            pulse_mod.reset_cache()
            status, raw = _get(
                self.port, "/pulse?url=http://127.0.0.1/latest/meta-data&src=https://evil.example"
            )
            self.assertEqual(status, 200)
            body = json.loads(raw)
            self.assertTrue(body.get("ok"))
            self.assertIn("chains", body)
            self.assertNotIn("listings", body)
            self.assertIn("samples", body)
            self.assertIsInstance(body["samples"], list)
            for chain in ("base", "solana", "algorand"):
                self.assertIn(chain, body["chains"])
                self.assertIn("themes", body["chains"][chain])
                self.assertIn("insight", body["chains"][chain])
            urlopen.assert_not_called()

    def test_pulse_fixture_samples(self):
        from live402 import pulse as pulse_mod
        pulse_mod.reset_cache()
        status, raw = _get(self.port, "/pulse")
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertIn("samples", body)
        self.assertIsInstance(body["samples"], list)
        self.assertGreaterEqual(len(body["samples"]), 1)
        for sample in body["samples"]:
            self.assertIn("need", sample)
            self.assertIn("label", sample)
            self.assertIn("url", sample)
            self.assertIn("price", sample)
            self.assertIn("chain", sample)
            self.assertTrue(str(sample["url"]).startswith("https://"))
            self.assertLessEqual(len(sample["need"]), 40)
            self.assertNotIn(".", sample["need"])
        needs = [s["need"].lower() for s in body["samples"]]
        self.assertTrue(any("weather" in n or "erc20" in n for n in needs))
        prices = {s["need"]: s["price"] for s in body["samples"]}
        self.assertTrue(any(v == "$0.01" for v in prices.values()))
        chains_in_samples = [s["chain"] for s in body["samples"]]
        self.assertIn("algorand", chains_in_samples)
        first_algo = next(i for i, c in enumerate(chains_in_samples) if c == "algorand")
        first_other = next((i for i, c in enumerate(chains_in_samples) if c != "algorand"), None)
        if first_other is not None:
            self.assertLess(first_algo, first_other)

    def test_discovery_docs_are_200(self):
        for path in (
            "/openapi.json",
            "/.well-known/x402",
            "/.well-known/x402.json",
            "/robots.txt",
            "/llms.txt",
        ):
            status, raw = _get(self.port, path)
            self.assertEqual(status, 200, path)
            self.assertTrue(raw.strip(), path)
        spec = json.loads(_get(self.port, "/openapi.json")[1])
        self.assertTrue(str(spec.get("openapi", "")).startswith("3."))
        route_get = spec["paths"]["/route"]["get"]
        self.assertEqual(route_get.get("summary"), "Get JSON 402 challenge or HTML page")
        self.assertIn("402", route_get["responses"])
        self.assertIn("200", route_get["responses"])
        route = spec["paths"]["/route"]["post"]
        self.assertIn("x-payment-info", route)
        self.assertIn("402", route["responses"])
        price = route["x-payment-info"]["price"]
        self.assertEqual(price["mode"], "fixed")
        self.assertEqual(price["amount"], "0.01")
        self.assertIn("x402", str(route["x-payment-info"]["protocols"]))
        wk = json.loads(_get(self.port, "/.well-known/x402")[1])
        wkj = json.loads(_get(self.port, "/.well-known/x402.json")[1])
        self.assertEqual(wk, wkj)
        self.assertIn("POST /route", str(wk.get("resources")))
        self.assertEqual(str(wk.get("price_usdc")), "0.01")
        amounts = [str(a.get("amount")) for a in wk.get("accepts") or []]
        self.assertEqual(amounts, ["10000", "10000", "10000"])
        robots = _get(self.port, "/robots.txt")[1]
        self.assertIn("User-agent:", robots)
        llms = _get(self.port, "/llms.txt")[1]
        self.assertIn("402Signal", llms)
        self.assertIn("$0.01", llms)
        self.assertIn("POST /route, not GET", llms)
        self.assertIn(
            "x402scan skips Algorand/GoPlausible; POST /route returns a currently-alive Algo 402 plus the target contract when that's what is live.",
            llms,
        )
        self.assertNotIn("Signal402", llms)
        self.assertIn("https://glama.ai/mcp/servers/402signal/402signal", llms)
        self.assertIn("https://smithery.ai/servers/live402/signal", llms)
        self.assertIn("api.cdp.coinbase.com/platform/v2/x402/discovery/search?query=402signal", llms)
        self.assertIn("facilitator.goplausible.xyz/discovery/resources", llms)
        self.assertIn("facilitator.goplausible.xyz/dashboard/merchants/56466a9400d70f08", llms)
        self.assertIn("www.x402scan.com/recipient/0xb18fc2275f36dae99eb215caeff03b431f887d16", llms)
        self.assertNotIn("dashboard/resources/UE9TVDpodHRwczovLzQwMnNpZ25hbC5jb20vcm91dGU", llms)
        self.assertNotIn("src=x402-global-challenge", llms)
        self.assertNotIn("24klabs.ai/listing/402signal", llms)


    def test_help_key_on_unpaid_402(self):
        status, body = _json_post(self.port, "/route", {"need": "weather"})
        self.assertEqual(status, 402)
        help_block = body.get("help") or {}
        self.assertEqual(help_block.get("docs"), "https://402signal.com/llms.txt")
        self.assertEqual(help_block.get("mcp"), "https://402signal.com/mcp.json")
        self.assertEqual(help_block.get("amount"), "$0.01")
        self.assertEqual(help_block.get("rails"), ["base", "solana", "algorand"])
        amounts = [str(a.get("amount")) for a in body.get("accepts") or []]
        self.assertEqual(amounts, ["10000", "10000", "10000"])

    def test_get_mcp_json_lists_route_tool(self):
        status, raw = _get(self.port, "/mcp.json")
        self.assertEqual(status, 200)
        body = json.loads(raw)
        names = [t.get("name") for t in body.get("tools") or []]
        self.assertIn("route", names)
        status2, raw2 = _get(self.port, "/.well-known/mcp.json")
        self.assertEqual(status2, 200)
        self.assertEqual(json.loads(raw2).get("tools"), body.get("tools"))
        status3, raw3 = _get(self.port, "/mcp")
        self.assertEqual(status3, 200)
        self.assertEqual(json.loads(raw3).get("tools"), body.get("tools"))

    def test_mcp_tools_list_free_and_call_unpaid_402(self):
        status, body = _json_post(
            self.port,
            "/mcp",
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
        )
        self.assertEqual(status, 200)
        self.assertIn("serverInfo", body.get("result") or {})
        status, body = _json_post(
            self.port,
            "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        self.assertEqual(status, 200)
        tools = ((body.get("result") or {}).get("tools")) or []
        self.assertTrue(any(t.get("name") == "route" for t in tools))
        status, body = _json_post(
            self.port,
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "route", "arguments": {"need": "weather"}},
            },
        )
        self.assertEqual(status, 402)
        self.assertIn("accepts", body)
        self.assertEqual(len(body.get("accepts") or []), 3)
        self.assertIn("help", body)
        amounts = [str(a.get("amount")) for a in body.get("accepts") or []]
        self.assertEqual(amounts, ["10000", "10000", "10000"])

    def test_desc_under_cdp_500(self):
        from live402 import discover
        self.assertLessEqual(len(discover.DESC), 500)
        self.assertIn("402", discover.DESC)
        self.assertIn("envelope", discover.DESC.lower())

    def test_empty_need_and_url_returns_402(self):
        """CDP validate POSTs empty JSON; must 402 with bazaar, not 400."""
        status, body = _json_post(self.port, "/route", {})
        self.assertEqual(status, 402)
        self.assertIn("accepts", body)
        self.assertEqual(len(body.get("accepts") or []), 3)
        bazaar = (body.get("extensions") or {}).get("bazaar") or {}
        self.assertIn("info", bazaar)
        self.assertEqual(bazaar["info"]["input"]["method"], "POST")

    def test_resource_url_pins_public_origin(self):
        """Host / fly.dev must not be reflected into payment resource or OpenAPI servers."""
        for host in ("402signal.fly.dev", "evil.example", "attacker.test"):
            conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
            body = json.dumps({"need": "erc20 token balance"}).encode("utf-8")
            conn.request(
                "POST",
                "/route",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Host": host,
                    "X-Forwarded-Proto": "https",
                },
            )
            res = conn.getresponse()
            raw = res.read()
            conn.close()
            self.assertEqual(res.status, 402, host)
            data = json.loads(raw.decode("utf-8"))
            self.assertEqual(data["resource"]["url"], "https://402signal.com/route", host)
            self.assertNotIn("fly.dev", json.dumps(data))
            self.assertNotIn(host, data["resource"]["url"])
        spec = json.loads(
            _get_full(
                self.port,
                "/openapi.json",
                extra_headers={"Host": "402signal.fly.dev"},
            )[1]
        )
        self.assertEqual(spec["servers"][0]["url"], "https://402signal.com")
        wk = json.loads(
            _get_full(
                self.port,
                "/.well-known/x402.json",
                extra_headers={"Host": "evil.example"},
            )[1]
        )
        self.assertEqual(wk.get("resource"), "https://402signal.com/route")


class FixtureProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["LOCAL_FREE"] = "1"
        os.environ["LIVE402_FIXTURE"] = "1"
        cls.httpd, cls.host, cls.port = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        os.environ.pop("LOCAL_FREE", None)

    def test_fixture_url_probe_live(self):
        status, body = _json_post(
            self.port,
            "/route",
            {
                "need": "weather",
                "url": "https://fixture.402signal.local/weather",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["live"])
        self.assertEqual(body["status"], 402)
        self.assertTrue(body["has_402_challenge"])
        self.assertEqual(body["latency_ms"], 41)
        self.assertIn("health", body)
        self.assertEqual(body["health"]["latency_ms"], 41)
        self.assertTrue(body["health"]["live"])

    def test_fixture_url_probe_dead_fail_closed(self):
        status, body = _json_post(
            self.port,
            "/route",
            {"need": "stale", "url": "https://fixture.402signal.local/weather-stale"},
        )
        self.assertEqual(status, 503)
        self.assertFalse(body["live"])
        self.assertEqual(body["tried"], 1)

    def test_fixture_need_returns_first_live(self):
        status, body = _json_post(
            self.port, "/route", {"need": "erc20 token balance"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["live"])
        self.assertEqual(body["url"], "https://fixture.402signal.local/erc20-balance")
        self.assertTrue(body["has_402_challenge"])
        self.assertGreaterEqual(body["tried"], 1)

    def test_fixture_need_fail_closed(self):
        status, body = _json_post(
            self.port, "/route", {"need": "zzzz-no-such-endpoint-xyzzy"}
        )
        self.assertEqual(status, 503)
        self.assertFalse(body["live"])
        self.assertEqual(body["tried"], 0)

    def test_echo_fixture_is_200_no_challenge_miss(self):
        status, body = _json_post(
            self.port,
            "/route",
            {"need": "echo", "url": "https://fixture.402signal.local/echo"},
        )
        self.assertEqual(status, 503)
        self.assertFalse(body["live"])
        self.assertEqual(body.get("miss_reason"), "reachable_200")
        self.assertEqual(body.get("status"), 200)
        self.assertFalse(body.get("has_402_challenge"))

    def test_fixture_402_envelope_has_payto_field(self):
        status, body = _json_post(
            self.port,
            "/route",
            {"need": "weather", "url": "https://fixture.402signal.local/weather"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["live"])
        self.assertEqual(body["status"], 402)
        self.assertTrue(body["has_402_challenge"])
        self.assertIn("payTo", body)
        self.assertEqual(body.get("traction"), "unknown")
        self.assertIn("probes", body)

    def test_empty_402_fixture_is_miss(self):
        status, body = _json_post(
            self.port,
            "/route",
            {"url": "https://fixture.402signal.local/empty-402"},
        )
        self.assertEqual(status, 503)
        self.assertFalse(body["live"])
        self.assertEqual(body.get("miss_reason"), "no_402_envelope")

    def test_post_only_402_fixture_is_live(self):
        status, body = _json_post(
            self.port,
            "/route",
            {"url": "https://fixture.402signal.local/post-only-402"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["live"])
        self.assertEqual(body["status"], 402)

    def test_unknown_https_url_fail_closed_no_network(self):
        status, body = _json_post(
            self.port,
            "/route",
            {"need": "x", "url": "https://this-should-not-be-fetched.example/nope"},
        )
        self.assertEqual(status, 503)
        self.assertFalse(body["live"])


class UnitHelpers(unittest.TestCase):
    def test_fixture_mode_on(self):
        self.assertTrue(fixtures.fixture_mode())

    def test_sample_need_from_path(self):
        from live402 import pulse as pulse_mod
        self.assertEqual(
            pulse_mod.sample_need_for(
                {"description": "Local weather forecast fixture"},
                "https://fixture.402signal.local/weather",
            ),
            "weather",
        )
        self.assertEqual(
            pulse_mod.sample_need_for(
                {"description": "ERC20 token balance probe fixture"},
                "https://fixture.402signal.local/erc20-balance",
            ),
            "erc20 token balance",
        )
        self.assertEqual(
            pulse_mod.sample_need_for(
                {},
                "https://api.paysponge.com/v0/inboxes/:inbox_id/messages",
            ),
            "inbox messages",
        )
        self.assertEqual(
            pulse_mod.sample_need_for(
                {},
                "https://api.onesource.io/api/chain/erc20-balance",
            ),
            "erc20 token balance",
        )
        self.assertIsNone(
            pulse_mod.sample_need_for(
                {},
                "https://weather.example/",
            )
        )
        self.assertIsNone(
            pulse_mod.sample_need_for(
                {},
                "https://x.example/0x1234567890abcdef1234567890abcdef12345678",
            )
        )

    def test_samples_prefer_useful_themes(self):
        from live402 import pulse as pulse_mod
        items = [
            {"url": "https://g.example/coinflip", "description": "coinflip game", "accepts": [{"amount": "10000"}]},
            {"url": "https://w.example/weather", "description": "weather", "accepts": [{"amount": "10000"}]},
            {"url": "https://x.example/erc20-balance", "description": "erc20", "accepts": [{"amount": "10000"}]},
        ]
        samples = pulse_mod._samples_for_items("base", items)
        needs = [s["need"] for s in samples]
        self.assertIn("weather", needs)
        self.assertTrue(any("erc20" in n for n in needs))
        self.assertFalse(any("coinflip" in n for n in needs))
        self.assertTrue(all(s["url"].startswith("https://") for s in samples))
        self.assertTrue(all(s["chain"] == "base" for s in samples))
        self.assertEqual(samples[0]["price"], "$0.01")
        only_game = pulse_mod._samples_for_items(
            "base",
            [{"url": "https://g.example/coinflip", "accepts": [{"amount": "10000"}]}],
        )
        self.assertEqual(len(only_game), 1)
        self.assertIn("coinflip", only_game[0]["need"])
        mixed = [
            {"url": f"https://x.example/erc20-balance-{i}", "accepts": [{"amount": "10000"}]}
            for i in range(4)
        ] + [
            {"url": "https://w.example/weather", "description": "weather", "accepts": [{"amount": "10000"}]},
            {"url": "https://s.example/search", "description": "web search", "accepts": [{"amount": "10000"}]},
        ]
        diverse = pulse_mod._samples_for_items("base", mixed)
        needs = [s["need"] for s in diverse]
        self.assertTrue(any("weather" in n for n in needs))
        self.assertTrue(any("search" in n for n in needs))
        self.assertTrue(any("erc20" in n for n in needs))
        self.assertLessEqual(len(diverse), 4)
        hinted = pulse_mod._samples_for_items(
            "base",
            [
                {"url": "https://k.example/api/v1/forecast/btc", "description": "ML forecast", "accepts": [{"amount": "10000"}]},
                {"url": "https://w.example/weather", "description": "live weather", "accepts": [{"amount": "10000"}]},
            ],
        )
        self.assertTrue(any(s["need"] == "weather" for s in hinted))

    def test_named_chain_token_match(self):
        from live402 import pulse as pulse_mod
        self.assertIsNone(pulse_mod.named_chain("weather"))
        self.assertIsNone(pulse_mod.named_chain("database weather"))
        self.assertIsNone(pulse_mod.named_chain("base or solana weather"))
        self.assertEqual(pulse_mod.named_chain("base weather"), "base")
        self.assertEqual(pulse_mod.named_chain("Algorand weather"), "algorand")
        self.assertEqual(pulse_mod.named_chain("solana search"), "solana")
        self.assertIsNone(pulse_mod.named_chain(""))

    def test_mixed_samples_algorand_first_real_urls(self):
        from live402 import pulse as pulse_mod
        chains = {
            "base": {
                "samples": [
                    {"need": "weather", "label": "weather", "url": "https://w.example/base-weather", "price": "$0.01", "chain": "base"},
                ]
            },
            "solana": {
                "samples": [
                    {"need": "search", "label": "search", "url": "https://s.example/sol-search", "price": "$0.01", "chain": "solana"},
                ]
            },
            "algorand": {
                "samples": [
                    {"need": "weather", "label": "weather", "url": "https://a.example/algo-weather", "price": "$0.01", "chain": "algorand"},
                ]
            },
        }
        mixed = pulse_mod._mixed_samples(chains)
        self.assertEqual([s["chain"] for s in mixed], ["algorand", "base", "solana"])
        self.assertTrue(all(str(s["url"]).startswith("https://") for s in mixed))
        empty_algo = pulse_mod._mixed_samples(
            {
                "base": {"samples": chains["base"]["samples"]},
                "solana": {"samples": chains["solana"]["samples"]},
                "algorand": {"samples": []},
            }
        )
        self.assertEqual([s["chain"] for s in empty_algo], ["base", "solana"])
        self.assertFalse(any("invent" in str(s["url"]) for s in empty_algo))
        self.assertEqual(pulse_mod._mixed_samples({}), [])

    def test_preview_weights_algorand_when_need_ambiguous(self):
        from live402 import pulse as pulse_mod
        payload = {
            "updated_at": "2026-08-30T00:00:00Z",
            "cached_s": 15,
            "samples": [
                {"need": "weather", "label": "weather", "url": "https://w.example/base-weather", "price": "$0.01", "chain": "base"},
                {"need": "weather", "label": "weather", "url": "https://a.example/algo-weather", "price": "$0.01", "chain": "algorand"},
                {"need": "search", "label": "search", "url": "https://s.example/sol-search", "price": "$0.01", "chain": "solana"},
            ],
            "chains": {},
        }
        with patch.object(pulse_mod, "get_pulse", return_value=payload):
            amb = pulse_mod.preview_need("weather")
            named_base = pulse_mod.preview_need("base weather")
            named_algo = pulse_mod.preview_need("algorand weather")
            named_sol = pulse_mod.preview_need("solana search")
        self.assertTrue(amb["not_probed"])
        self.assertNotIn("live", amb)
        self.assertTrue(all("live" not in h for h in amb["hits"]))
        self.assertGreaterEqual(len(amb["hits"]), 2)
        self.assertEqual(amb["hits"][0]["chain"], "algorand")
        self.assertEqual(amb["hits"][0]["url"], "https://a.example/algo-weather")
        self.assertTrue(any(h["chain"] == "base" for h in amb["hits"]))
        self.assertTrue(all(h["chain"] == "base" for h in named_base["hits"]))
        self.assertTrue(all(h["chain"] == "algorand" for h in named_algo["hits"]))
        self.assertTrue(named_sol["not_probed"])
        self.assertTrue(all(h["chain"] == "solana" for h in named_sol["hits"]))
        self.assertEqual(named_sol["hits"][0]["url"], "https://s.example/sol-search")

    def test_theme_buckets(self):
        from live402 import pulse as pulse_mod
        weather = {"description": "Local weather forecast fixture", "url": "https://fixture.402signal.local/weather"}
        self.assertEqual(pulse_mod.theme_id_for(weather, weather["url"]), "weather")
        erc = {"description": "ERC20 token balance probe", "url": "https://x.example/erc20"}
        self.assertEqual(pulse_mod.theme_id_for(erc, erc["url"]), "onchain")
        echo = {"description": "Open echo ping", "url": "https://fixture.402signal.local/echo"}
        self.assertEqual(pulse_mod.theme_id_for(echo, echo["url"]), "other")

    def test_theme_id_from_url_path(self):
        from live402 import pulse as pulse_mod
        cases = [
            ({"url": "https://api.paysponge.com/v0/inboxes/:inbox_id/messages"}, "messaging"),
            ({"url": "https://stableupload.dev/api/upload"}, "storage"),
            ({"url": "https://laso.finance/get-card"}, "payments"),
            ({"url": "https://coinflip402.vercel.app/api/coinflip"}, "games"),
            ({"url": "https://api.syraa.fun/insights/defi-tvl"}, "market"),
            ({"url": "https://onestepchess.xyz/api/v1/moves"}, "games"),
            ({"url": "https://api.dev.hypercli.com/agents/x402/solo"}, "compute"),
        ]
        for item, want in cases:
            self.assertEqual(
                pulse_mod.theme_id_for(item, item["url"]),
                want,
                item["url"],
            )

    def test_theme_stem_lite_and_no_chain_name(self):
        from live402 import pulse as pulse_mod
        self.assertEqual(
            pulse_mod.theme_id_for({"url": "https://x.example/messages"}, "https://x.example/messages"),
            "messaging",
        )
        self.assertEqual(
            pulse_mod.theme_id_for({"url": "https://x.example/mcp"}, "https://x.example/mcp"),
            "compute",
        )
        self.assertEqual(
            pulse_mod.theme_id_for({"url": "https://x.example/solana"}, "https://x.example/solana"),
            "other",
        )
        self.assertEqual(
            pulse_mod.theme_id_for({"url": "https://x.example/algorand"}, "https://x.example/algorand"),
            "other",
        )
        # TLD .ai is not compute; path token ai-visibility is.
        self.assertEqual(
            pulse_mod.theme_id_for({"url": "https://nansen.ai/api/perp"}, "https://nansen.ai/api/perp"),
            "other",
        )
        self.assertEqual(
            pulse_mod.theme_id_for(
                {"url": "https://citable.run/v1/ai-visibility"},
                "https://citable.run/v1/ai-visibility",
            ),
            "compute",
        )
        self.assertEqual(
            pulse_mod._stem_lite({"gas", "ens", "news", "messages"}),
            {"gas", "ens", "news", "messages", "message"},
        )

    def test_insight_does_not_claim_named_lead_when_other_dominates(self):
        from live402 import pulse as pulse_mod
        themes = [
            {"id": "other", "label": "other", "count": 58, "share": 0.58},
            {"id": "search", "label": "search", "count": 11, "share": 0.11},
        ]
        text = pulse_mod._insight("solana", 99, themes)
        self.assertIn("unlabeled", text.lower())
        self.assertNotIn("leads", text)
        self.assertNotIn("search", text.lower())

    def test_other_theme_keeps_five_https_examples(self):
        from live402 import pulse as pulse_mod
        items = [{"url": f"https://plain.example/item{i}"} for i in range(6)]
        items.append({"url": "https://w.example/weather"})
        total, themes = pulse_mod._themes_for_items(items)
        self.assertEqual(total, 7)
        by_id = {t["id"]: t for t in themes}
        self.assertEqual(len(by_id["other"]["examples"]), 5)
        self.assertTrue(all(u.startswith("https://") for u in by_id["other"]["examples"]))
        self.assertLessEqual(len(by_id["weather"]["examples"]), 3)
        self.assertGreater(by_id["other"].get("unlabeled", 0), 0)
        self.assertNotIn("search", {t["id"] for t in themes if t["count"] <= 0})

    def test_parse_envelope_empty_402(self):
        env, miss = probe.parse_envelope(402, {}, b"{}")
        self.assertIsNone(env)
        self.assertEqual(miss, "no_402_envelope")
        env, miss = probe.parse_envelope(200, {}, b'{"ok":true}')
        self.assertIsNone(env)
        self.assertEqual(miss, "reachable_200")
        env, miss = probe.parse_envelope(
            402, {}, b'{"x402Version":2,"accepts":[{"payTo":"0xabc"}]}'
        )
        self.assertIsNotNone(env)
        self.assertIsNone(miss)
        self.assertEqual(probe._payto_from_envelope(env), "0xabc")
        env, miss = probe.parse_envelope(402, {}, b'{"x402Version":2,"accepts":[]}')
        self.assertIsNone(env)
        self.assertEqual(miss, "no_402_envelope")
        env, miss = probe.parse_envelope(
            402, {}, b'{"x402Version":2,"accepts":[{"network":"base"}]}'
        )
        self.assertIsNone(env)
        self.assertEqual(miss, "no_payto")


    def test_rank_prefer_network_and_settlements(self):
        ranked = probe.rank_resources(
            "weather forecast", fixtures.load_resources(), prefer_network="algorand"
        )
        urls = [probe._resource_url(r) for r in ranked]
        self.assertTrue(urls)
        self.assertIn("/algorand/weather", urls[0])
        high = {
            "url": "https://hi.example/weather",
            "description": "weather forecast",
            "x402Requests": 9000,
            "accepts": [{"network": "eip155:8453", "payTo": "0xabc", "amount": "10000"}],
        }
        low = {
            "url": "https://lo.example/weather",
            "description": "weather forecast",
            "x402Requests": 1,
            "accepts": [{"network": "eip155:8453", "payTo": "0xabc", "amount": "10000"}],
        }
        ranked = probe.rank_resources("weather", [low, high])
        self.assertEqual(probe._resource_url(ranked[0]), high["url"])

    def test_skip_localhost_and_param_templates(self):
        self.assertTrue(probe.skip_candidate_url("https://localhost/weather"))
        self.assertTrue(probe.skip_candidate_url("https://127.0.0.1/weather"))
        self.assertTrue(probe.skip_candidate_url("https://api.example/v0/inboxes/:inbox_id/messages"))
        self.assertTrue(probe.skip_candidate_url("https://api.example/users/{id}"))
        self.assertFalse(probe.skip_candidate_url("https://api.example/weather"))
        from live402 import pulse as pulse_mod
        samples = pulse_mod._samples_for_items(
            "base",
            [
                {"url": "https://localhost/weather", "description": "weather", "accepts": [{"amount": "10000"}]},
                {"url": "https://api.paysponge.com/v0/inboxes/:inbox_id/messages", "accepts": [{"amount": "10000"}]},
                {"url": "https://w.example/weather", "description": "weather", "accepts": [{"amount": "10000"}]},
            ],
        )
        urls = [s["url"] for s in samples]
        self.assertTrue(all("localhost" not in u for u in urls))
        self.assertTrue(all(":inbox_id" not in u for u in urls))
        self.assertTrue(any(u.endswith("/weather") for u in urls))

    def test_rank_weather(self):
        ranked = probe.rank_resources("weather forecast", fixtures.load_resources())
        urls = [probe._resource_url(r) for r in ranked]
        self.assertTrue(urls)
        self.assertIn("https://fixture.402signal.local/weather", urls[0])


def _payment_signature(overrides=None):
    payload = {
        "x402Version": 2,
        "accepted": {
            "scheme": "exact",
            "network": "base",
            "asset": "USDC",
            "currency": payment.USDC_BASE,
            "amount": payment.AMOUNT_ATOMIC,
            "payTo": payment.DEFAULT_PAYTO,
            "maxTimeoutSeconds": 60,
        },
        "payload": {
            "signature": "0x" + ("ab" * 65),
            "authorization": {
                "from": "0x1111111111111111111111111111111111111111",
                "to": payment.DEFAULT_PAYTO,
                "value": payment.AMOUNT_ATOMIC,
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x" + ("11" * 32),
            },
        },
        "extensions": {"bazaar": payment.BAZAAR_EXTENSION},
    }
    if overrides:
        payload.update(overrides)
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _fake_facilitator(url, body, headers=None, timeout=20.0):
    _ = headers, timeout, body
    if str(url).rstrip("/").endswith("/verify"):
        return 200, {"isValid": True, "payer": "0x1111111111111111111111111111111111111111"}
    if str(url).rstrip("/").endswith("/settle"):
        return 200, {
            "success": True,
            "transaction": "0x" + ("cd" * 32),
            "network": "eip155:8453",
            "payer": "0x1111111111111111111111111111111111111111",
        }
    return 404, {"error": "unexpected_facilitator_url"}


class PaidFacilitatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("LOCAL_FREE", None)
        os.environ["LIVE402_FIXTURE"] = "1"
        # Dummy Bearer so Base rail reaches the mocked facilitator (not a wallet key).
        os.environ["CDP_ACCESS_TOKEN"] = "test-fixture-token"
        cls.httpd, cls.host, cls.port = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        os.environ.pop("CDP_ACCESS_TOKEN", None)

    def test_mocked_verify_settle_opens_gate(self):
        with patch("live402.facilitator.post_json", side_effect=_fake_facilitator) as mock_post:
            status, body = _json_post(
                self.port,
                "/route",
                {
                    "need": "weather",
                    "url": "https://fixture.402signal.local/weather",
                },
                extra_headers={"PAYMENT-SIGNATURE": _payment_signature()},
            )
            self.assertEqual(status, 200)
            self.assertTrue(body["live"])
            self.assertEqual(body["status"], 402)
            self.assertTrue(body["has_402_challenge"])
            self.assertEqual(body["url"], "https://fixture.402signal.local/weather")
            self.assertIn("payTo", body)
            self.assertEqual(body.get("traction"), "unknown")
            urls = [call.args[0] for call in mock_post.call_args_list]
            self.assertTrue(any(u.endswith("/verify") for u in urls))
            self.assertTrue(any(u.endswith("/settle") for u in urls))
            self.assertEqual(len(urls), 2)
            # bazaar echoed on settle body
            settle_body = mock_post.call_args_list[1].args[1]
            bazaar = ((settle_body.get("paymentPayload") or {}).get("extensions") or {}).get(
                "bazaar"
            )
            self.assertTrue(bazaar)
            self.assertIn("info", bazaar)

    def test_paid_empty_body_returns_400_without_settle(self):
        """After mock verify, empty body is 400 and settle is skipped."""
        with patch("live402.facilitator.post_json", side_effect=_fake_facilitator) as mock_post:
            status, body = _json_post(
                self.port,
                "/route",
                {},
                extra_headers={"PAYMENT-SIGNATURE": _payment_signature()},
            )
            self.assertEqual(status, 400)
            self.assertIn("need", body.get("error", "").lower())
            urls = [call.args[0] for call in mock_post.call_args_list]
            self.assertTrue(any(str(u).rstrip("/").endswith("/verify") for u in urls))
            self.assertFalse(any(str(u).rstrip("/").endswith("/settle") for u in urls))

    def test_failed_verify_does_not_probe(self):
        def reject(url, body, headers=None, timeout=20.0):
            _ = body, headers, timeout
            if str(url).endswith("/verify"):
                return 200, {"isValid": False, "invalidReason": "invalid_payload"}
            raise AssertionError("settle must not run after failed verify")

        with patch("live402.facilitator.post_json", side_effect=reject), patch(
            "live402.probe.probe_url"
        ) as mock_url, patch("live402.probe.route_need") as mock_need:
            status, body = _json_post(
                self.port,
                "/route",
                {"need": "weather", "url": "https://fixture.402signal.local/weather"},
                extra_headers={"PAYMENT-SIGNATURE": _payment_signature()},
            )
            self.assertEqual(status, 402)
            self.assertNotEqual(body.get("live"), True)
            mock_url.assert_not_called()
            mock_need.assert_not_called()


class SsrfTests(unittest.TestCase):
    def test_safe_target_rejects_http_loopback(self):
        self.assertIsNone(probe.safe_target("http://127.0.0.1"))
        self.assertIsNone(probe.safe_target("http://127.0.0.1/"))
        self.assertIsNone(probe.safe_target("https://127.0.0.1"))
        self.assertIsNone(probe.safe_target("https://localhost"))
        self.assertIsNone(probe.safe_target("https://169.254.169.254/latest/meta-data"))
        self.assertIsNone(probe.safe_target("https://10.0.0.1"))
        self.assertIsNone(probe.safe_target("https://192.168.1.1"))
        self.assertIsNone(probe.safe_target("https://172.16.0.1"))
        self.assertIsNone(probe.safe_target("https://[::1]/"))
        self.assertIsNone(probe.safe_target("http://example.com"))

    def test_probe_url_does_not_open_loopback(self):
        with patch("live402.probe.fixtures.fixture_mode", return_value=False), patch(
            "live402.probe._opener"
        ) as opener:
            result = probe.probe_url("http://127.0.0.1")
            self.assertFalse(result.get("live"))
            self.assertEqual(result.get("miss_reason"), "ssrf")
            opener.assert_not_called()
            result = probe.probe_url("https://127.0.0.1")
            self.assertFalse(result.get("live"))
            self.assertEqual(result.get("miss_reason"), "ssrf")
            opener.assert_not_called()

    def test_getaddrinfo_timeout_fail_closed(self):
        def hang(*_a, **_k):
            time.sleep(5)
            return []

        with patch("live402.probe.DNS_TIMEOUT", 0.25), patch(
            "socket.getaddrinfo", side_effect=hang
        ):
            t0 = time.monotonic()
            self.assertFalse(probe._resolve_public("this-must-not-hang.invalid"))
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.5)
        self.assertGreaterEqual(elapsed, 0.2)


class FacilitatorFailClosedTests(unittest.TestCase):
    def test_call_rejects_http_4xx_even_if_isValid(self):
        with patch(
            "live402.facilitator.post_json",
            return_value=(400, {"isValid": True, "success": True}),
        ):
            result = facilitator._call(
                "solana",
                "https://facilitator.payai.network/verify",
                {},
                1.0,
            )
            self.assertFalse(result.ok)

    def test_verify_requires_isValid_true(self):
        accept = {
            "scheme": "exact",
            "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
            "asset": "USDC",
            "amount": payment.AMOUNT_ATOMIC,
            "payTo": payment.DEFAULT_PAYTO_SOLANA,
        }
        payload = {"accepted": accept, "payload": {}}
        with patch("live402.facilitator.post_json", return_value=(200, {})):
            result = facilitator.verify(payload, accept)
            self.assertFalse(result.ok)
        with patch(
            "live402.facilitator.post_json",
            return_value=(200, {"isValid": True}),
        ):
            result = facilitator.verify(payload, accept)
            self.assertTrue(result.ok)



class PulseAllowlistTests(unittest.TestCase):
    def test_catalog_hosts_are_exactly_the_three(self):
        hosts = set(probe.CATALOG_HOSTS)
        self.assertEqual(
            hosts,
            {
                "api.cdp.coinbase.com",
                "facilitator.payai.network",
                "facilitator.goplausible.xyz",
            },
        )
        for _rail, url in probe.CATALOGS:
            self.assertTrue(probe.catalog_url_allowed(url))

    def test_catalog_url_allowed_fail_closed(self):
        self.assertFalse(probe.catalog_url_allowed("http://api.cdp.coinbase.com/platform/v2/x402/discovery/resources?limit=20"))
        self.assertFalse(probe.catalog_url_allowed("https://evil.example/discovery/resources"))
        self.assertFalse(probe.catalog_url_allowed("https://127.0.0.1/"))
        self.assertFalse(probe.catalog_url_allowed("https://169.254.169.254/latest/meta-data"))
        self.assertFalse(probe.catalog_url_allowed("https://localhost/discovery"))
        self.assertFalse(probe.catalog_url_allowed("https://api.cdp.coinbase.com:443@127.0.0.1/"))

    def test_fetch_catalog_never_opens_non_allowlisted(self):
        from live402 import pulse as pulse_mod
        with patch("urllib.request.urlopen") as urlopen, patch(
            "live402.probe._catalog_opener"
        ) as opener:
            self.assertEqual(pulse_mod._fetch_catalog("evil", "https://127.0.0.1/secret"), [])
            self.assertEqual(pulse_mod._fetch_catalog("evil", "https://evil.example/x"), [])
            self.assertEqual(pulse_mod._fetch_catalog("base", "http://api.cdp.coinbase.com/x"), [])
            urlopen.assert_not_called()
            opener.assert_not_called()

    def test_price_atomic_is_cents_not_dollars(self):
        from live402 import pulse as pulse_mod
        label, usd = pulse_mod.usdc_atomic_to_price("10000")
        self.assertEqual(label, "$0.01")
        self.assertAlmostEqual(usd, 0.01)
        label, usd = pulse_mod.usdc_atomic_to_price("100000")
        self.assertEqual(label, "$0.10")
        self.assertAlmostEqual(usd, 0.10)
        self.assertNotIn("10000", label)

    def test_ssrf_still_blocks_private_https(self):
        self.assertIsNone(probe.safe_target("https://127.0.0.1"))
        self.assertIsNone(probe.safe_target("https://10.0.0.1/x"))
        self.assertIsNone(probe.safe_target("http://example.com"))


class RateLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("LOCAL_FREE", None)
        os.environ["LIVE402_ROUTE_RPM"] = "2"
        os.environ["LIVE402_ROUTE_RPM_FACILITATOR"] = "8"
        cls.httpd, cls.host, cls.port = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        os.environ.pop("LIVE402_ROUTE_RPM", None)
        os.environ.pop("LIVE402_ROUTE_RPM_FACILITATOR", None)

    def test_route_rate_limit_429(self):
        ip_headers = {"Fly-Client-IP": "203.0.113.50"}
        statuses = []
        for _ in range(3):
            status, _body = _json_post(
                self.port, "/route", {"need": "weather"}, extra_headers=ip_headers
            )
            statuses.append(status)
        self.assertEqual(statuses[0], 402)
        self.assertEqual(statuses[1], 402)
        self.assertEqual(statuses[2], 429)

    def test_facilitator_ua_gets_higher_burst(self):
        headers = {
            "Fly-Client-IP": "203.0.113.51",
            "User-Agent": "Coinbase-CDP-x402-crawler/1.0",
        }
        statuses = []
        for _ in range(3):
            status, _body = _json_post(
                self.port, "/route", {}, extra_headers=headers
            )
            statuses.append(status)
        self.assertEqual(statuses, [402, 402, 402])

    def test_preview_uses_separate_looser_limiter(self):
        ip_headers = {"Fly-Client-IP": "203.0.113.52"}
        statuses = []
        for _ in range(3):
            status, _body = _json_post(
                self.port, "/route", {"need": "weather"}, extra_headers=ip_headers
            )
            statuses.append(status)
        self.assertEqual(statuses[2], 429)
        status, raw, _hdrs = _get_full(
            self.port, "/preview?need=weather", extra_headers=ip_headers
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw).get("not_probed"))
        status, body = _json_post(
            self.port,
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "preview", "arguments": {"need": "weather"}},
            },
            extra_headers=ip_headers,
        )
        self.assertEqual(status, 200)
        self.assertTrue(body.get("not_probed"))



class PreviewRateLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("LOCAL_FREE", None)
        os.environ["LIVE402_PREVIEW_RPM"] = "2"
        cls.httpd, cls.host, cls.port = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        os.environ.pop("LIVE402_PREVIEW_RPM", None)

    def test_preview_rate_limit_429(self):
        ip_headers = {"Fly-Client-IP": "203.0.113.60"}
        statuses = []
        for _ in range(3):
            status, _raw, _hdrs = _get_full(
                self.port, "/preview?need=weather", extra_headers=ip_headers
            )
            statuses.append(status)
        self.assertEqual(statuses[0], 200)
        self.assertEqual(statuses[1], 200)
        self.assertEqual(statuses[2], 429)

    def test_mcp_preview_shares_preview_limiter(self):
        ip_headers = {"Fly-Client-IP": "203.0.113.61"}
        statuses = []
        payload = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "preview", "arguments": {"need": "weather"}},
        }
        for _ in range(3):
            status, _body = _json_post(
                self.port, "/mcp", payload, extra_headers=ip_headers
            )
            statuses.append(status)
        self.assertEqual(statuses[0], 200)
        self.assertEqual(statuses[1], 200)
        self.assertEqual(statuses[2], 429)
        # Paid /route still uses the route limiter, not this preview bucket.
        status, body = _json_post(
            self.port, "/route", {}, extra_headers=ip_headers
        )
        self.assertEqual(status, 402)
        amounts = [str(a.get("amount")) for a in body.get("accepts") or []]
        self.assertEqual(amounts, ["10000", "10000", "10000"])


class ProductBriefTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("LOCAL_FREE", None)
        os.environ["LIVE402_FIXTURE"] = "1"
        cls.httpd, cls.host, cls.port = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_preview_unpaid_200(self):
        from live402 import pulse as pulse_mod
        pulse_mod.reset_cache()
        with patch("live402.probe.probe_url") as mock_url, patch(
            "live402.probe.route_need"
        ) as mock_need:
            status, raw = _get(self.port, "/preview?need=weather")
            self.assertEqual(status, 200)
            body = json.loads(raw)
            self.assertTrue(body.get("not_probed"))
            self.assertEqual(body.get("need"), "weather")
            self.assertIn("hits", body)
            self.assertIn("freshness", body)
            self.assertIsInstance(body["hits"], list)
            self.assertNotIn("live", body)
            if body["hits"]:
                self.assertEqual(body["hits"][0].get("chain"), "algorand")
                self.assertTrue(str(body["hits"][0].get("url") or "").startswith("https://"))
                self.assertNotIn("live", body["hits"][0])
            mock_url.assert_not_called()
            mock_need.assert_not_called()

    def test_head_discovery_paths_are_200(self):
        for path in (
            "/llms.txt",
            "/openapi.json",
            "/mcp.json",
            "/preview?need=weather",
            "/rails",
            "/pulse",
        ):
            status, body, headers = _head(self.port, path)
            self.assertEqual(status, 200, path)
            self.assertEqual(body, b"", path)
            self.assertTrue(headers.get("content-length"), path)
            self.assertNotEqual(headers.get("content-length"), "0", path)

    def test_preview_prefer_network_and_hit_fields(self):
        from live402 import pulse as pulse_mod
        pulse_mod.reset_cache()
        status, raw = _get(self.port, "/preview?need=weather&prefer_network=algorand")
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertTrue(body.get("not_probed"))
        self.assertIn("hits", body)
        if body["hits"]:
            hit = body["hits"][0]
            self.assertEqual(hit.get("chain"), "algorand")
            for key in ("facilitator", "method", "inputSchema_present", "rails_up"):
                self.assertIn(key, hit)
            self.assertNotIn("live", hit)
            if hit.get("facilitator"):
                self.assertNotIn("x402.org", str(hit.get("facilitator")).lower())

    def test_rails_200(self):
        status, raw = _get(self.port, "/rails")
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertTrue(body.get("ok"))
        self.assertEqual(str(body.get("amountAtomic")), "10000")
        self.assertEqual(body.get("asset"), "USDC")
        rails = body.get("rails") or []
        self.assertEqual(len(rails), 3)
        names = [r.get("network") for r in rails]
        self.assertEqual(names, ["base", "solana", "algorand"])
        for row in rails:
            self.assertIn("facilitator", row)
            self.assertIn("amountAtomic", row)
            self.assertEqual(str(row.get("amountAtomic")), "10000")
            self.assertIn("maxTimeoutSeconds", row)
            self.assertIn("up", row)
            self.assertIn("latency_ms", row)
            self.assertNotIn("x402.org", str(row.get("facilitator") or "").lower())
        self.assertEqual(len(body.get("facilitators") or []), 3)
        self.assertIn("feePayers", body)
        health_status, health_raw = _get(self.port, "/health")
        self.assertEqual(health_status, 200)
        self.assertEqual(json.loads(health_raw), {"ok": True})
        self.assertNotIn("rails", json.loads(health_raw))

    def test_miss_reason_enum(self):
        from live402.probe import MISS_REASONS, public_miss_reason
        expected = {
            "no_candidates",
            "no_402_envelope",
            "no_payto",
            "reachable_200",
            "probe_timeout",
            "quote_expired",
            "invalid_need",
            "upstream_5xx",
            "ssrf",
            "no_input_schema",
            "constraints_unmet",
        }
        self.assertEqual(set(MISS_REASONS), expected)
        self.assertEqual(public_miss_reason("empty_402"), "no_402_envelope")
        self.assertEqual(public_miss_reason("http_200_no_challenge"), "reachable_200")
        self.assertEqual(public_miss_reason("timeout"), "probe_timeout")
        self.assertEqual(public_miss_reason("no_match"), "no_candidates")
        self.assertEqual(public_miss_reason("http_503"), "upstream_5xx")
        self.assertEqual(public_miss_reason("ssrf"), "ssrf")
        for key in expected:
            self.assertIn(public_miss_reason(key), expected)

    def test_mcp_output_schema(self):
        from live402 import mcp as mcp_mod
        tools = mcp_mod.manifest()["tools"]
        route = next(t for t in tools if t.get("name") == "route")
        self.assertLessEqual(len(route["description"]), 200)
        self.assertIn("HTTP 402", route["description"])
        self.assertIn("honest miss", route["description"])
        self.assertNotIn("Signal402", route["description"])
        self.assertEqual(route["inputSchema"].get("required"), ["need"])
        self.assertIn("prefer_network", (route["inputSchema"].get("properties") or {}))
        props = (route.get("outputSchema") or {}).get("properties") or {}
        for key in ("live", "url", "invocable", "target", "miss_reason", "tried", "latency_ms", "schema_source"):
            self.assertIn(key, props)
        bazaar = (mcp_mod.handle_mcp(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "route", "arguments": {"need": "weather"}}},
            {},
            "https://402signal.com/mcp",
        )[1].get("extensions") or {}).get("bazaar") or {}
        inp = (bazaar.get("info") or {}).get("input") or {}
        self.assertEqual(inp.get("type"), "mcp")
        self.assertEqual(inp.get("toolName"), "route")

    def test_openapi_preview_and_rails(self):
        spec = json.loads(_get(self.port, "/openapi.json")[1])
        self.assertIn("/preview", spec["paths"])
        self.assertIn("get", spec["paths"]["/preview"])
        self.assertIn("/rails", spec["paths"])
        self.assertIn("get", spec["paths"]["/rails"])
        self.assertEqual(spec["info"]["contact"]["email"], "402signal@gmail.com")
        self.assertIn("feePayer", spec["info"]["x-guidance"])
        self.assertIn("eip155:8453", spec["info"]["x-guidance"])
        self.assertIn("x402.org", spec["info"]["x-guidance"])
        post_402 = spec["paths"]["/route"]["post"]["responses"]["402"]["content"]["application/json"]["example"]
        self.assertIn("help", post_402)
        self.assertIn("bazaar", (post_402.get("extensions") or {}))
        self.assertEqual(post_402.get("network"), "base")
        need_req = spec["paths"]["/route"]["post"]["requestBody"]["content"]["application/json"]["schema"].get("required")
        self.assertEqual(need_req, ["need"])
        probes = spec["paths"]["/route"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["probes"]
        self.assertIn("items", probes)
        for path, methods in spec["paths"].items():
            for method, op in methods.items():
                if not isinstance(op, dict):
                    continue
                summary = op.get("summary") or ""
                self.assertGreaterEqual(len(summary), 24, "%s %s %r" % (method, path, summary))
                self.assertLessEqual(len(summary), 63, "%s %s %r" % (method, path, summary))
                self.assertNotIn("free", summary.lower(), "%s %s" % (method, path))

    def test_preview_ignores_caller_url(self):
        with patch("live402.pulse._fetch_catalog") as mock_fetch, patch(
            "urllib.request.urlopen"
        ) as urlopen:
            status, raw = _get(
                self.port,
                "/preview?need=weather&url=http://127.0.0.1/latest/meta-data",
            )
            self.assertEqual(status, 200)
            body = json.loads(raw)
            self.assertTrue(body.get("not_probed"))
            mock_fetch.assert_not_called()
            urlopen.assert_not_called()

    def test_root_mcp_json_is_remote_no_secrets(self):
        path = os.path.join(os.path.dirname(__file__), "..", ".mcp.json")
        with open(path, encoding="utf-8") as fh:
            card = json.load(fh)
        server = (card.get("mcpServers") or {}).get("402Signal") or {}
        self.assertEqual(server.get("url"), "https://402signal.com/mcp")
        self.assertEqual(server.get("type"), "streamable-http")
        blob = json.dumps(card).lower()
        for banned in ("secret", "api_key", "apikey", "token", "password", "authorization"):
            self.assertNotIn(banned, blob)

    def test_mcp_preview_unpaid_200(self):
        status, body = _json_post(
            self.port,
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "preview", "arguments": {"need": "weather"}},
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(body.get("not_probed"))
        self.assertIn("hits", body)


class FixtureTargetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["LOCAL_FREE"] = "1"
        os.environ["LIVE402_FIXTURE"] = "1"
        cls.httpd, cls.host, cls.port = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        os.environ.pop("LOCAL_FREE", None)

    def test_target_object_on_live_route_without_schema(self):
        status, body = _json_post(
            self.port,
            "/route",
            {
                "need": "weather",
                "url": "https://fixture.402signal.local/weather",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["live"])
        self.assertFalse(body.get("invocable"))
        self.assertEqual(body.get("miss_reason"), "no_input_schema")
        target = body.get("target")
        self.assertIsInstance(target, dict)
        for key in (
            "method",
            "inputSchema",
            "outputSchema",
            "accepts",
            "facilitator",
            "amountAtomic",
            "displayAmount",
            "timeoutSeconds",
        ):
            self.assertIn(key, target)

    def test_target_object_on_live_route_with_schema(self):
        status, body = _json_post(
            self.port, "/route", {"need": "erc20 token balance"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["live"])
        self.assertTrue(body.get("invocable"))
        self.assertEqual(body.get("schema_source"), "bazaar")
        target = body.get("target")
        self.assertIsInstance(target, dict)
        self.assertIsInstance(target.get("inputSchema"), dict)
        self.assertTrue(target["inputSchema"].get("properties") or target["inputSchema"].get("required"))
        accepts = target.get("accepts") or []
        self.assertTrue(accepts)
        fac = (accepts[0].get("extra") or {}).get("facilitator")
        self.assertIsInstance(fac, dict)
        self.assertTrue(str(fac.get("url") or "").startswith("https://"))
        self.assertNotIn("x402.org", str(fac.get("url") or "").lower())
        self.assertEqual(str(target.get("amountAtomic")), "10000")


def _patch_ping_open(side_effect):
    opener = unittest.mock.Mock()
    opener.open.side_effect = side_effect
    return patch("live402.rails._ping_opener", return_value=opener), opener


class RailsPingTests(unittest.TestCase):
    def test_http_error_401_is_up(self):
        from io import BytesIO
        from live402 import rails as rails_mod
        from live402 import payment
        url = rails_mod._supported_url(payment.CDP_FACILITATOR)
        self.assertTrue(probe.catalog_url_allowed(url))
        err = urllib.error.HTTPError(url, 401, "Unauthorized", hdrs=None, fp=BytesIO(b""))
        ctx, opener = _patch_ping_open(err)
        with ctx:
            up, latency = rails_mod._ping(url)
        opener.open.assert_called_once()
        self.assertTrue(up)
        self.assertIsInstance(latency, int)
        self.assertGreaterEqual(latency, 0)

    def test_http_error_503_is_down(self):
        from io import BytesIO
        from live402 import rails as rails_mod
        from live402 import payment
        url = rails_mod._supported_url(payment.CDP_FACILITATOR)
        err = urllib.error.HTTPError(url, 503, "Service Unavailable", hdrs=None, fp=BytesIO(b""))
        ctx, _opener = _patch_ping_open(err)
        with ctx:
            up, latency = rails_mod._ping(url)
        self.assertFalse(up)
        self.assertIsInstance(latency, int)

    def test_ping_does_not_follow_redirect(self):
        from io import BytesIO
        from live402 import rails as rails_mod
        from live402 import payment
        url = rails_mod._supported_url(payment.CDP_FACILITATOR)
        dest = "https://127.0.0.1/secret"
        opened = []

        def fake_open(req, timeout=None):
            full = req.get_full_url()
            opened.append(full)
            raise urllib.error.HTTPError(
                full, 302, "Found", hdrs={"Location": dest}, fp=BytesIO(b"do-not-parse")
            )

        ctx, _opener = _patch_ping_open(fake_open)
        with ctx:
            up, latency = rails_mod._ping(url)
        self.assertFalse(up)
        self.assertIsInstance(latency, int)
        self.assertEqual(opened, [url])
        self.assertFalse(any("127.0.0.1" in u for u in opened))
        handler = rails_mod._NoRedirectHandler()
        req = urllib.request.Request(url)
        self.assertIsNone(handler.redirect_request(req, None, 302, "Found", {}, dest))
        opener = rails_mod._ping_opener()
        self.assertTrue(any(isinstance(h, rails_mod._NoRedirectHandler) for h in opener.handlers))

    def test_urlerror_timeout_is_down(self):
        from live402 import rails as rails_mod
        from live402 import payment
        url = rails_mod._supported_url(payment.CDP_FACILITATOR)
        ctx, _opener = _patch_ping_open(urllib.error.URLError("timed out"))
        with ctx:
            up, latency = rails_mod._ping(url)
        self.assertFalse(up)
        self.assertIsInstance(latency, int)

    def test_http_and_non_allowlisted_never_open(self):
        from live402 import rails as rails_mod
        ctx, opener = _patch_ping_open(AssertionError("must not open"))
        with ctx:
            up, latency = rails_mod._ping("http://api.cdp.coinbase.com/platform/v2/x402/supported")
            self.assertFalse(up)
            self.assertIsNone(latency)
            opener.open.assert_not_called()
            up, latency = rails_mod._ping("https://evil.example/supported")
            self.assertFalse(up)
            self.assertIsNone(latency)
            opener.open.assert_not_called()


class PublicRateLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("LOCAL_FREE", None)
        os.environ["LIVE402_PUBLIC_RPM"] = "2"
        cls.httpd, cls.host, cls.port = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        os.environ.pop("LIVE402_PUBLIC_RPM", None)

    def test_pulse_rate_limit_429(self):
        ip_headers = {"Fly-Client-IP": "203.0.113.70"}
        statuses = []
        for _ in range(3):
            status, _raw, _hdrs = _get_full(
                self.port, "/pulse", extra_headers=ip_headers
            )
            statuses.append(status)
        self.assertEqual(statuses[0], 200)
        self.assertEqual(statuses[1], 200)
        self.assertEqual(statuses[2], 429)

    def test_rails_rate_limit_429(self):
        ip_headers = {"Fly-Client-IP": "203.0.113.71"}
        statuses = []
        for _ in range(3):
            status, _raw, _hdrs = _get_full(
                self.port, "/rails", extra_headers=ip_headers
            )
            statuses.append(status)
        self.assertEqual(statuses[0], 200)
        self.assertEqual(statuses[1], 200)
        self.assertEqual(statuses[2], 429)

    def test_health_unlimited_after_pulse_burst(self):
        ip_headers = {"Fly-Client-IP": "203.0.113.72"}
        for _ in range(3):
            _get_full(self.port, "/pulse", extra_headers=ip_headers)
        status, raw, _hdrs = _get_full(
            self.port, "/health", extra_headers=ip_headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"ok": True})
        status, raw, _hdrs = _get_full(
            self.port, "/rails", extra_headers=ip_headers
        )
        self.assertEqual(status, 200)
        status, raw, _hdrs = _get_full(
            self.port, "/preview?need=weather", extra_headers=ip_headers
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw).get("not_probed"))




if __name__ == "__main__":
    unittest.main()
