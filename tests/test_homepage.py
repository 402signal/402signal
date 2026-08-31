"""Homepage product-site tests. Frontend + copy only."""

import json
import os
import unittest
from html.parser import HTMLParser
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading

os.environ.setdefault("LIVE402_FIXTURE", "1")
os.environ.pop("LOCAL_FREE", None)

from live402.server import Handler


STATIC = Path(__file__).resolve().parent.parent / "live402" / "static"


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return httpd, host, port


def _get_full(port, path, extra_headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = dict(extra_headers or {})
    conn.request("GET", path, headers=headers)
    res = conn.getresponse()
    raw = res.read()
    hdrs = {k.lower(): v for k, v in res.getheaders()}
    conn.close()
    return res.status, raw.decode("utf-8"), hdrs


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._parts = []
        self._in_a = False

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href")
        self._href = href
        self._parts = []
        self._in_a = True

    def handle_data(self, data):
        if self._in_a:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._in_a:
            self.links.append(("".join(self._parts).strip(), self._href))
            self._in_a = False
            self._href = None


def _links(html):
    parser = _LinkParser()
    parser.feed(html)
    return parser.links


def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


class HomepageProductTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("LOCAL_FREE", None)
        os.environ["LIVE402_FIXTURE"] = "1"
        cls.httpd, cls.host, cls.port = _serve()
        cls.html = _get_full(cls.port, "/")[1]
        cls.js = _read("app.js")
        cls.css = _read("styles.css")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_new_hero_copy_present(self):
        html = self.html
        self.assertIn("Find a paid API that works right now.", html)
        self.assertIn(
            "402Signal searches x402 services across Base, Solana, and Algorand, then checks the endpoint at request time before returning it to your agent.",
            html,
        )
        self.assertIn("If we can't verify a usable payment path, we return a miss instead of guessing.", html)
        self.assertIn("Directories tell an agent what is listed. 402Signal records what the endpoint actually returned.", html)
        self.assertIn("Works means the payment interface was ready when we checked.", html)
        self.assertIn("does not mean 402Signal paid the seller", html)
        self.assertIn('href="#try"', html)
        self.assertIn('href="#integrate"', html)
        self.assertIn("Search services", html)
        self.assertIn("Integrate 402Signal", html)
        self.assertIn("402Signal - Find a paid API that works right now", html)
        self.assertIn("independently checks payment endpoints", html)

    def test_old_hero_slogans_gone(self):
        html = self.html
        self.assertNotIn("Check before your agent pays", html)
        self.assertNotIn("Catalog said X. We observed Y.", html)
        self.assertNotIn("How you'd use this", html)
        self.assertNotIn("Front of a larger process", html)
        self.assertIn("How agents use it", html)
        self.assertNotIn(">Shared<", html)
        self.assertNotIn("Honest miss", html)
        self.assertNotIn("not_probed", html)

    def test_probe_and_wallet_absent(self):
        html = self.html
        js = self.js
        self.assertNotIn("Probe live", html)
        self.assertNotIn('id="probe-btn"', html)
        self.assertNotIn('id="pay-base"', html)
        self.assertNotIn("pay-base", html)
        self.assertNotIn("Pay $0.01 on Base", html)
        self.assertNotIn("injected wallet", html.lower())
        self.assertNotIn("window.ethereum", js)
        self.assertNotIn("payBase", js)
        self.assertNotIn("eth_signTypedData_v4", js)
        self.assertNotIn("wallet_switchEthereumChain", js)
        self.assertNotIn("eip155:8453", js)
        self.assertNotIn("Pay $0.01 on Base", js)

    def test_free_catalog_search_uses_preview(self):
        status, raw, _hdrs = _get_full(self.port, "/preview?need=weather")
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertTrue(body.get("not_probed"))
        self.assertIn("hits", body)
        self.assertIn('fetch("/preview', self.js)
        self.assertIn('id="search-btn"', self.html)
        self.assertIn("Search the x402 catalog", self.html)
        self.assertIn("What does your agent need?", self.html)
        self.assertIn(">Search<", self.html)
        self.assertNotIn(">Preview<", self.html)
        self.assertIn("Results have not been live-probed unless explicitly marked otherwise.", self.html)

    def test_catalog_results_not_labeled_verified_or_recommended(self):
        js = self.js
        html = self.html
        self.assertIn("Catalog match", js)
        self.assertIn("Not live-verified", js)
        self.assertNotIn("Recommended", js)
        self.assertNotIn("Recommended", html)
        self.assertNotIn("Payable now", js)
        self.assertNotIn("Payable now", html)
        self.assertNotIn("Verified", js.replace("Not live-verified", ""))
        self.assertIn("Listed price", js)
        self.assertIn("Catalog payTo", js)
        self.assertIn("Schema listed", js)
        self.assertIn("No catalog matches found. Try a broader capability.", js)
        self.assertIn("Catalog data is refreshing. Try again shortly.", js)

    def test_post_endpoints_not_misleading_get_links(self):
        html = self.html
        hrefs = [href or "" for _text, href in _links(html)]
        for href in hrefs:
            self.assertFalse(href.rstrip("/").endswith("/route"), href)
            self.assertFalse(href.rstrip("/").endswith("/validate"), href)
            self.assertFalse(href.rstrip("/") == "https://402signal.com/mcp", href)
            self.assertFalse(href.rstrip("/") == "/mcp", href)
        self.assertIn("<code>POST /route</code>", html)
        self.assertIn("<code>POST /validate</code>", html)
        self.assertIn("<code>https://402signal.com/mcp</code>", html)
        self.assertNotIn('href="https://402signal.com/route"', html)
        self.assertNotIn('href="/route"', html)
        self.assertNotIn('href="/validate"', html)
        self.assertNotIn('href="https://402signal.com/mcp"', html)

    def test_inline_link_styling_consistent(self):
        css = self.css
        self.assertIn("text-decoration-thickness", css)
        self.assertIn("text-underline-offset", css)
        self.assertIn("a:link", css)
        self.assertIn("a:visited", css)
        self.assertIn("a:hover, a:focus", css)
        self.assertIn("var(--accent)", css)
        self.assertIn("var(--fox)", css)
        self.assertIn("var(--teal)", css)
        self.assertNotIn(".trust { color: var(--accent);", css)

    def test_provenance_ui_does_not_fallback_catalog_as_observed(self):
        js = self.js
        self.assertIn("function independentlyObserved", js)
        self.assertIn("402signal_observed", js)
        self.assertNotIn("observed.payTo || parsed.payTo", js)
        self.assertNotIn("parsed.payTo ||", js)
        self.assertNotIn("parsed.target && parsed.target.inputSchema", js)
        self.assertNotIn("target.inputSchema", js)
        self.assertNotIn("parsed.invocable === true", js)
        self.assertNotIn("parsed.status", js)
        self.assertNotIn("parsed.latency_ms", js)
        self.assertIn("observedField", js)
        self.assertIn("hasOwnProperty.call(obs, key)", js)

    def test_unknown_preserved(self):
        js = self.js
        self.assertIn('return "Unknown"', js)
        self.assertIn('return "unknown"', js)
        self.assertIn("schema_present: Unknown", js)
        html = self.html
        self.assertIn("Missing evidence is UNKNOWN, not no.", html)
        self.assertIn("UNKNOWN is better than a guess.", html)

    def test_pulse_shown_or_hidden_per_usefulness(self):
        html = self.html
        js = self.js
        self.assertIn('id="pulse"', html)
        self.assertRegex(html, r'id="pulse"[^>]*\bhidden\b')
        self.assertIn('id="nav-pulse"', html)
        self.assertRegex(html, r'id="nav-pulse"[^>]*\bhidden\b')
        self.assertIn("function pulseUseful", js)
        self.assertIn("function pulseCatalogCounts", js)
        self.assertIn("index_status", js)
        self.assertIn("pending", js)
        self.assertIn("refreshing", js)
        self.assertIn('fetch("/pulse"', js)
        self.assertNotIn("healthy", js)
        self.assertNotIn("executable_now_rate", js)
        self.assertNotIn("success_7d", js)
        self.assertNotIn("n_7d", js)
        self.assertNotIn("7d reliability", html)
        self.assertNotIn("healthy", html)
        self.assertNotIn("Executable Now Rate", html)
        self.assertIn("catalog listings, not 402Signal observations", html)

    def test_no_preview_route_mcp_openapi_regression(self):
        for path in (
            "/preview?need=weather",
            "/openapi.json",
            "/mcp.json",
            "/.well-known/x402.json",
            "/rails",
            "/llms.txt",
        ):
            status, raw, hdrs = _get_full(self.port, path)
            self.assertEqual(status, 200, path)
            self.assertTrue(raw.strip(), path)
        status, raw, _hdrs = _get_full(self.port, "/preview?need=weather")
        body = json.loads(raw)
        self.assertTrue(body.get("not_probed"))
        spec = json.loads(_get_full(self.port, "/openapi.json")[1])
        self.assertIn("/preview", spec["paths"])
        self.assertIn("/route", spec["paths"])
        self.assertIn("/mcp", spec.get("paths") or spec["paths"])
        mcp = json.loads(_get_full(self.port, "/mcp.json")[1])
        names = [t.get("name") for t in mcp.get("tools") or []]
        self.assertIn("route", names)
        self.assertIn("preview", names)
        mcp_status, mcp_raw, _mcp_hdrs = _get_full(self.port, "/mcp")
        self.assertIn(mcp_status, (200, 400, 402, 405, 406))
        self.assertTrue(mcp_raw.strip())
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "POST",
            "/route",
            json.dumps({"need": "weather"}).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
        res = conn.getresponse()
        route_raw = res.read()
        conn.close()
        self.assertEqual(res.status, 402)
        route_body = json.loads(route_raw.decode("utf-8"))
        self.assertIn("accepts", route_body)

    def test_nav_and_ia(self):
        html = self.html
        self.assertIn(">Product<", html)
        self.assertIn(">Try it<", html)
        self.assertIn(">Integrate<", html)
        self.assertIn(">GitHub<", html)
        self.assertNotIn("class=\"sep\"", html)
        self.assertIn("Search the x402 catalog", html)
        self.assertIn("Where it fits", html)
        self.assertIn("What gets checked", html)
        self.assertIn("How agents use it", html)
        self.assertIn("Catalog claims and 402Signal observations", html)
        self.assertIn("Quickstart", html)
        self.assertIn("<h2>Rails</h2>", html)
        self.assertIn("id=\"copy-curl\"", html)
        self.assertIn("aria-live=\"polite\"", html)
        self.assertIn("Building an x402 API? Check whether your endpoint is agent-ready.", html)
        self.assertIn("compared candidates", html)
        self.assertIn("no qualifying endpoint was verified", html)
        self.assertNotIn("x402scan lists Solana", html)
        self.assertNotIn("skips Algorand", html)

    def test_no_wallet_or_local_free(self):
        for name in ("index.html", "app.js", "styles.css"):
            text = _read(name)
            self.assertNotIn("LOCAL_FREE", text)
            self.assertNotIn("mnemonic", text.lower())


if __name__ == "__main__":
    unittest.main()
