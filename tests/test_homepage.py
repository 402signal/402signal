"""Human-page IA tests. Frontend + copy only."""

import json
import os
import re
import tempfile
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

NAV_LABELS = ("How it works", "Developers")
LISTED_ON = (
    ("Glama", "https://glama.ai/mcp/servers/402signal/402signal"),
    ("MCP Registry", "https://registry.modelcontextprotocol.io/?q=402signal"),
    ("Gold-402", "https://github.com/Haustorium12/gold-402/blob/main/directory/aggregators.md"),
    ("Smithery", "https://smithery.ai/servers/live402/signal"),
    ("Agentic Market", "https://agentic.market/services/402signal-com"),
    ("x402-dev", "https://github.com/michielpost/x402-dev/blob/master/Projects.md"),
    ("GoPlausible", "https://facilitator.goplausible.xyz/dashboard/bazaar?q=402signal"),
)
BANNED = (
    "Where it fits",
    "At the intersection of",
    "Powerful",
    "Seamless",
    "Robust",
    "Unlock",
    "Next-generation",
    "Revolutionary",
    "Game-changing",
    "The real value",
    "The key difference",
    "The important thing is",
    "This is where",
    "In today's",
    "In an increasingly",
    "Designed to",
    "Built to empower",
    "Bridge the gap",
    "Route + evidence",
    "UNKNOWN is better than a guess",
    "Honest miss",
)
OLD_HOME_SECTIONS = (
    "Works means the payment interface was ready when we checked.",
    "Directories tell an agent what is listed.",
    "Search the x402 catalog",
    "Free catalog search",
    "Where it fits",
    "What gets checked",
    "How agents use it",
    "Catalog claims and 402Signal observations",
    "Quickstart",
    "<h2>Rails</h2>",
    "Building an x402 API?",
    "Machine-readable",
    "Need→Candidates",
    "id=\"search-form\"",
    "id=\"copy-curl\"",
    "miss_reason",
    "PAYMENT-SIGNATURE",
    "accepts[]",
)


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


class _DocParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.h1 = []
        self.nav_links = []
        self.listed_links = []
        self.listed_imgs = 0
        self._href = None
        self._parts = []
        self._in_a = False
        self._in_h1 = False
        self._h1_parts = []
        self._in_nav = False
        self._nav_depth = 0
        self._in_listed = False
        self._listed_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "nav":
            self._in_nav = True
            self._nav_depth = 1
        elif self._in_nav:
            self._nav_depth += 1
        if tag == "p" and "listed-on" in classes:
            self._in_listed = True
            self._listed_depth = 1
        elif self._in_listed:
            self._listed_depth += 1
        if tag == "img" and self._in_listed:
            self.listed_imgs += 1
        if tag == "a":
            self._href = attrs.get("href")
            self._parts = []
            self._in_a = True
        if tag == "h1":
            self._in_h1 = True
            self._h1_parts = []

    def handle_data(self, data):
        if self._in_a:
            self._parts.append(data)
        if self._in_h1:
            self._h1_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._in_a:
            text = "".join(self._parts).strip()
            self.links.append((text, self._href))
            if self._in_nav:
                self.nav_links.append((text, self._href))
            if self._in_listed:
                self.listed_links.append((text, self._href))
            self._in_a = False
            self._href = None
        if tag == "h1" and self._in_h1:
            self.h1.append("".join(self._h1_parts).strip())
            self._in_h1 = False
        if self._in_nav:
            self._nav_depth -= 1
            if self._nav_depth <= 0:
                self._in_nav = False
        if self._in_listed:
            self._listed_depth -= 1
            if self._listed_depth <= 0:
                self._in_listed = False


def _parse(html):
    parser = _DocParser()
    parser.feed(html)
    return parser


def _links(html):
    return _parse(html).links


def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def _strip_head(html):
    return re.sub(r"<head\b.*?</head>", "", html, flags=re.I | re.S)


class HomepageProductTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("LOCAL_FREE", None)
        os.environ["LIVE402_FIXTURE"] = "1"
        cls._pq_tmp = tempfile.TemporaryDirectory()
        os.environ["LIVE402_PQ_LOG_DB"] = os.path.join(cls._pq_tmp.name, "pq-log.sqlite")
        from live402.pq import store as pq_store

        pq_store.reset()
        cls.httpd, cls.host, cls.port = _serve()
        cls.home = _get_full(cls.port, "/")[1]
        cls.catalog = _get_full(cls.port, "/catalog")[1]
        cls.how = _get_full(cls.port, "/how")[1]
        cls.devs = _get_full(cls.port, "/developers")[1]
        cls.transparency = _get_full(cls.port, "/transparency")[1]
        cls.js = _read("app.js")
        cls.css = _read("styles.css")
        cls.pages = {
            "/": cls.home,
            "/catalog": cls.catalog,
            "/how": cls.how,
            "/developers": cls.devs,
            "/transparency": cls.transparency,
        }

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        from live402.pq import store as pq_store

        pq_store.reset()
        os.environ.pop("LIVE402_PQ_LOG_DB", None)
        cls._pq_tmp.cleanup()

    def test_homepage_is_concise_product_landing(self):
        html = self.home
        parsed = _parse(html)
        self.assertEqual(parsed.h1, ["Find a paid API that works right now."])
        self.assertNotIn("<h1>402Signal</h1>", html)
        self.assertEqual(html.count("<h1"), 1)
        self.assertIn("402Signal: Find a paid API that works right now", html)
        self.assertIn(
            "402Signal independently checks x402 payment endpoints before an agent relies on them. Base, Solana and Algorand.",
            html,
        )
        self.assertIn(
            "Independent check before spend. Catalogs are candidates, not truth. Your agent keeps the wallet. History is Falcon-anchored on Algorand TestNet.",
            html,
        )
        self.assertIn("Base · Solana · Algorand", html)
        self.assertIn("Try 402Signal", html)
        self.assertIn("Developer docs", html)
        self.assertIn('href="/catalog"', html)
        self.assertIn('href="/developers"', html)
        self.assertIn("Fresh, not just listed", html)
        self.assertIn("Policy actually matters", html)
        self.assertIn("A miss is a valid answer", html)
        self.assertIn("Your wallet stays yours", html)
        self.assertIn("weather on Base, at most $0.05", html)
        self.assertIn("Discoverable via", html)
        self.assertIn("Discovery listings, not endorsements.", html)
        self.assertIn("$0.01 USDC per live routing check · your agent keeps the wallet.", html)
        self.assertIn("What catalogs claim", html)
        self.assertIn("Check it now", html)
        self.assertIn("DISCOVER", html)
        self.assertIn("Find candidates", html)
        self.assertIn("PROBE", html)
        self.assertIn("Check the endpoint now", html)
        self.assertIn("OBSERVE", html)
        self.assertIn("Capture current x402 behavior", html)
        self.assertIn("COMPARE", html)
        self.assertIn("Apply constraints and rank", html)
        self.assertIn("Valid x402? Payment terms? Invocation info? Fresh observation?", html)
        self.assertIn("Decide whether to spend", html)
        self.assertIn('class="signal-flow"', html)
        self.assertIn('class="trust-rail"', html)
        self.assertIn("Algorand TestNet · Falcon-1024", html)
        self.assertEqual(html.count('class="signal-flow"'), 1)
        self.assertNotIn('id="decision"', html)
        self.assertNotIn("A decision, with evidence", html)
        self.assertNotIn(">ROUTE<", html)
        self.assertNotIn(">MISS<", html)
        self.assertNotIn(">HISTORY<", html)
        self.assertIn('id="why"', html)
        self.assertIn("decision-grid", html)
        self.assertNotIn("Browse the catalog", html)
        self.assertNotIn("Use 402Signal in an agent", html)
        self.assertNotIn("<pre", html)
        self.assertNotIn("<code>", html)
        self.assertNotIn("Latest checkpoint", html)
        self.assertNotIn("Falcon", _parse(html).h1[0])

    def test_old_one_page_sections_gone_from_home(self):
        html = self.home
        for snippet in OLD_HOME_SECTIONS:
            self.assertNotIn(snippet, html, snippet)
        self.assertNotIn(">Product<", html)
        self.assertNotIn(">Try it<", html)
        self.assertNotIn(">Integrate<", html)
        self.assertNotIn(">Pulse<", html)
        self.assertNotIn('href="/pulse"', html)

    def test_exactly_one_h1_per_human_page(self):
        expected = {
            "/": "Find a paid API that works right now.",
            "/catalog": "Browse x402 services",
            "/how": "Why check an API that's already listed?",
            "/developers": "Use 402Signal from an agent",
            "/transparency": "Verify 402Signal’s history.",
        }
        for path, title in expected.items():
            parsed = _parse(self.pages[path])
            self.assertEqual(parsed.h1, [title], path)

    def test_primary_nav_on_every_page(self):
        for path, html in self.pages.items():
            parsed = _parse(html)
            labels = [text for text, _href in parsed.nav_links]
            self.assertEqual(labels, list(NAV_LABELS), path)
            hrefs = [href for _text, href in parsed.nav_links]
            self.assertEqual(
                hrefs,
                [
                    "/how",
                    "/developers",
                ],
                path,
            )
            self.assertNotIn("Catalog", labels)
            self.assertNotIn("GitHub", labels)
            self.assertNotIn("Transparency", labels)
            self.assertIn('class="mark"', html)
            self.assertIn('class="brand-name"', html)
            self.assertIn(">402Signal<", html)
            self.assertNotIn(">Product<", html)
            self.assertNotIn(">Pulse<", html)
            self.assertNotIn('class="sep"', html)
            self.assertNotIn(" | ", _parse(html).nav_links[0][0] if parsed.nav_links else "")

    def test_github_link_works(self):
        for path, html in self.pages.items():
            self.assertIn('href="https://github.com/402signal/402signal"', html, path)

    def test_catalog_page_search_uses_preview(self):
        html = self.catalog
        self.assertIn("Browse x402 services", html)
        self.assertIn("Search discovery listings across Base, Solana and Algorand.", html)
        self.assertIn("Prior check, not this request.", html)
        self.assertIn("Discovery listings are candidates.", html)
        self.assertIn("OBSERVED is a prior 402Signal check, not this request.", html)
        self.assertIn("Before spending, use paid /route.", html)
        self.assertNotIn("PQ", html)
        self.assertNotIn("Falcon", html)
        self.assertIn("What does your agent need?", html)
        self.assertIn('id="search-form"', html)
        self.assertIn('id="search-btn"', html)
        self.assertIn(">Search<", html)
        self.assertIn('data-need="web search"', html)
        self.assertIn('data-need="weather"', html)
        self.assertIn('data-need="token risk"', html)
        self.assertIn('data-need="LLM inference"', html)
        self.assertIn('data-need="wallet balance"', html)
        self.assertEqual(html.lower().count("free catalog search"), 0)
        self.assertIn("x402 API Catalog · 402Signal", html)
        status, raw, _hdrs = _get_full(self.port, "/preview?need=weather")
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertTrue(body.get("not_probed"))
        self.assertIn("hits", body)
        self.assertIn('fetch("/preview', self.js)

    def test_catalog_results_are_compact_catalog_only(self):
        js = self.js
        self.assertIn("result-row", js)
        self.assertIn("Listed price", js)
        self.assertIn("Schema listed", js)
        self.assertIn("DISCOVERED · ", js)
        self.assertIn("discovery matches", js)
        self.assertIn("matches returned by discovery", js)
        self.assertIn(" shown", js)
        self.assertIn("DISCOVERED · catalog listing", js)
        self.assertIn("OBSERVED · prior 402Signal check", js)
        self.assertIn("Not yet observed", js)
        self.assertNotIn("Catalog match", js)
        self.assertNotIn("Not live-verified", js)
        self.assertNotIn("Recommended", js)
        self.assertNotIn("Payable now", js)
        self.assertNotIn("Verified", js)
        self.assertNotIn("8 verified", js)
        self.assertNotIn("independentlyObserved", js)
        self.assertNotIn("402signal_observed", js)
        self.assertNotIn("candidates_probed", js)
        self.assertNotIn("probe_budget_exhausted", js)
        self.assertNotIn("observed.payTo || parsed.payTo", js)
        self.assertNotIn("parsed.payTo ||", js)
        self.assertNotIn("target.inputSchema", js)
        self.assertNotIn("parsed.invocable === true", js)
        self.assertNotIn("success_7d", js)
        self.assertIn("observations in 7d", js)
        self.assertNotIn(" in 7d · ", js)
        self.assertIn("No catalog matches found. Try a broader capability.", js)
        self.assertIn("Catalog data is refreshing. Try again shortly.", js)
        self.assertNotIn("MIN_RELIABILITY_N", js)
        self.assertNotIn("claimed-vs-observed", self.catalog)
        self.assertNotIn("Catalog claims", self.catalog)
        self.assertNotIn("verified provider", (self.catalog + js).lower())
        self.assertNotIn("trusted merchant", (self.catalog + js).lower())
        self.assertNotIn("quality verified", (self.catalog + js).lower())
        self.assertNotIn("healthy", js)

    def test_how_page_renders(self):
        html = self.how
        self.assertIn("Why check an API that's already listed?", html)
        self.assertIn("Directory data can outlive the service it describes.", html)
        self.assertIn("402Signal keeps the listing and the runtime observation separate.", html)
        self.assertIn("What a live check can establish", html)
        self.assertIn("Did the endpoint return a valid, parseable x402 challenge?", html)
        self.assertIn(">PAYMENT<", html)
        self.assertIn(">TERMS<", html)
        self.assertIn(">INVOCATION<", html)
        self.assertIn(">FRESHNESS<", html)
        self.assertIn(">EVIDENCE<", html)
        self.assertIn(">Readiness<", html)
        self.assertIn("Catalogs supply candidates.", html)
        self.assertIn("402Signal sits between discovery and spend.", html)
        self.assertIn("Find candidates → check now → apply policy → route or honest miss.", html)
        self.assertIn("402Signal vs your agent", html)
        self.assertIn("Keeps the wallet, signs, pays the seller, and calls the API.", html)
        self.assertNotIn("See the flow on the", html)
        self.assertNotIn('class="signal-flow"', html)
        self.assertNotIn("Trust path", html)
        self.assertIn("Found in supported discovery infrastructure.", html)
        self.assertIn("402Signal stops before seller execution", html)
        self.assertIn("402Signal recommends a route.", html)
        self.assertIn("A check should leave a trail", html)
        self.assertIn("See the transparency log", html)
        self.assertIn('href="/transparency"', html)
        self.assertIn("How 402Signal Works", html)
        self.assertNotIn("Where it fits", html)
        self.assertNotIn("HEALTHY", html)

    def test_developers_page_renders(self):
        html = self.devs
        self.assertIn("Use 402Signal from an agent", html)
        self.assertIn("Send the capability you need. Pay $0.01 USDC for the live routing check.", html)
        self.assertIn("Your agent keeps the wallet.", html)
        self.assertIn("<details", html)
        self.assertIn(">HTTP<", html)
        self.assertIn(">MCP<", html)
        self.assertIn("<code>POST /route</code>", html)
        self.assertIn("id=\"copy-curl\"", html)
        self.assertIn("curl -sS -D - https://402signal.com/route", html)
        self.assertIn("<code>https://402signal.com/mcp</code>", html)
        self.assertIn("402Signal Developer API", html)
        self.assertIn('href="/openapi.json"', html)
        self.assertIn('href="/llms.txt"', html)
        self.assertIn('href="/mcp.json"', html)
        self.assertIn('href="/.well-known/x402.json"', html)
        self.assertIn('href="/rails"', html)
        self.assertIn('href="/attestation"', html)
        self.assertIn('href="/pulse"', html)
        self.assertIn("selected_payment", html)
        self.assertIn("probe_ceiling", html)
        self.assertIn("claimed vs observed", html.lower())
        self.assertIn("lowest_total_cost", html)
        self.assertIn("fastest_settlement", html)
        self.assertIn("prefer_network", html)
        hrefs = [href or "" for _text, href in _links(html)]
        for href in hrefs:
            self.assertFalse(href.rstrip("/").endswith("/route"), href)
            self.assertFalse(href.rstrip("/").endswith("/validate"), href)
            self.assertFalse(href.rstrip("/") == "https://402signal.com/mcp", href)
            self.assertFalse(href.rstrip("/") == "/mcp", href)
        self.assertNotIn("PQ Trust", html)
        self.assertNotIn("post-quantum", html.lower())
        self.assertNotIn("algo_bonus", html)
        self.assertNotIn("TestNet Falcon broadcast is off unless explicitly enabled.", html)
        self.assertIn("402Signal maintains a public append-only transparency log", html)
        self.assertIn("Confirmed checkpoints are currently anchored to Algorand TestNet", html)
        self.assertIn("The human-readable transparency view is available at", html)
        self.assertIn('href="/transparency"', html)

    def test_listed_on_footer_verified_only(self):
        forbidden = (
            "facilitator.goplausible.xyz/dashboard/merchants",
            "x402scan.com/recipient",
            "www.x402scan.com",
            "Merchant record",
            "402index.io",
            "api.cdp.coinbase.com",
            "facilitator.payai.network",
        )
        expected = list(LISTED_ON)
        for path, html in self.pages.items():
            parsed = _parse(html)
            if path == "/developers":
                self.assertIn("Listed on", html, path)
                self.assertIn("<details", html, path)
                self.assertEqual(parsed.listed_links, expected, path)
                self.assertEqual(parsed.listed_imgs, 0, path)
                self.assertEqual(html.count("listed-on"), 1, path)
            elif path == "/":
                self.assertIn("Discoverable via", html, path)
                self.assertNotIn("Listed on", html, path)
                self.assertIn("Discovery listings, not endorsements.", html, path)
                self.assertEqual(parsed.listed_links, expected, path)
                self.assertEqual(parsed.listed_imgs, 0, path)
                self.assertEqual(html.count("listed-on"), 1, path)
            else:
                self.assertNotIn("Listed on", html, path)
                self.assertNotIn("Discoverable via", html, path)
                self.assertNotIn("listed-on", html, path)
                self.assertEqual(parsed.listed_links, [], path)
            for blob in forbidden:
                self.assertNotIn(blob, html, path)
            self.assertIn("mailto:ross@402signal.com", html, path)
            self.assertNotIn("mailto:402signal@gmail.com", html, path)
            self.assertIn(">ross@402signal.com<", html, path)
            self.assertNotIn(">Contact<", html, path)
            self.assertIn("https://x.com/402Signal", html, path)
            self.assertIn('href="/openapi.json"', html, path)
            self.assertIn(">OpenAPI<", html, path)
            self.assertIn('href="/mcp.json"', html, path)
            self.assertIn(">MCP<", html, path)
            self.assertIn('href="/transparency"', html, path)
            self.assertIn(">Transparency<", html, path)
            self.assertIn('href="/catalog"', html, path)
            self.assertIn("https://github.com/402signal/402signal", html, path)

    def test_probe_and_wallet_absent(self):
        for name in ("index.html", "catalog.html", "how.html", "developers.html", "app.js", "transparency.js"):
            text = _read(name)
            self.assertNotIn("Probe live", text)
            self.assertNotIn('id="probe-btn"', text)
            self.assertNotIn("Pay $0.01 on Base", text)
            self.assertNotIn("injected wallet", text.lower())
            self.assertNotIn("window.ethereum", text)
            self.assertNotIn("LOCAL_FREE", text)
            self.assertNotIn("mnemonic", text.lower())

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

    def test_mobile_no_horizontal_overflow(self):
        css = self.css
        self.assertIn("overflow-x: hidden", css)
        self.assertIn("@media (max-width: 720px)", css)
        self.assertIn("@media (max-width: 640px)", css)
        self.assertIn("@media (max-width: 390px)", css)
        self.assertIn("@media (max-width: 320px)", css)
        self.assertIn(".signal-flow", css)
        self.assertIn(".trust-rail", css)
        self.assertIn(".status-grid", css)
        self.assertIn(".copy-btn", css)
        self.assertIn(".mobile-only", css)
        self.assertIn(".desktop-only", css)
        self.assertIn(".sr-only", css)
        self.assertIn(".flow-ops", css)
        self.assertIn("grid-template-columns: 1fr", css)
        for html in self.pages.values():
            self.assertIn('name="viewport"', html)
            self.assertIn("width=device-width", html)

    def test_banned_marketing_language(self):
        for path, html in self.pages.items():
            for phrase in BANNED:
                self.assertNotIn(phrase, html, f"{path}: {phrase}")
            self.assertNotIn("\N{EM DASH}", html, path)

    def test_no_em_dash_on_all_human_pages(self):
        extra = {
            "/route": _get_full(self.port, "/route", extra_headers={"Accept": "text/html"})[1],
            "/dashboard": _get_full(self.port, "/dashboard")[1],
        }
        for path, html in {**self.pages, **extra}.items():
            self.assertNotIn("\N{EM DASH}", html, path)
        for name in ("app.js", "dashboard.js", "transparency.js"):
            text = _read(name)
            self.assertNotIn("\N{EM DASH}", text, name)

    def test_authored_human_sources_have_no_em_dash(self):
        root = Path(__file__).resolve().parent.parent
        sources = (
            "live402/static/index.html",
            "live402/static/catalog.html",
            "live402/static/how.html",
            "live402/static/developers.html",
            "live402/static/route.html",
            "live402/static/app.js",
            "live402/static/dashboard.js",
            "live402/static/transparency.js",
            "live402/pq/transparency.py",
            "live402/site_chrome.py",
        )
        for rel in sources:
            text = (root / rel).read_text(encoding="utf-8")
            self.assertNotIn("\N{EM DASH}", text, rel)
        from live402 import pulse

        self.assertNotIn("\N{EM DASH}", pulse.dashboard_html())

    def test_human_pages_are_static_html_same_csp(self):
        for path in ("/", "/catalog", "/how", "/developers", "/transparency"):
            status, raw, hdrs = _get_full(self.port, path)
            self.assertEqual(status, 200, path)
            self.assertIn("text/html", hdrs.get("content-type", ""), path)
            self.assertTrue(raw.strip(), path)
            csp = hdrs.get("content-security-policy") or ""
            self.assertEqual(
                csp,
                "default-src 'none'; script-src 'self'; "
                "connect-src 'self'; "
                "style-src 'self'; img-src 'self' data:; base-uri 'self'; "
                "frame-ancestors 'none'",
            )

    def test_no_preview_route_mcp_openapi_regression(self):
        for path in (
            "/preview?need=weather",
            "/openapi.json",
            "/mcp.json",
            "/.well-known/x402.json",
            "/rails",
            "/pulse",
            "/llms.txt",
        ):
            status, raw, hdrs = _get_full(self.port, path)
            self.assertEqual(status, 200, path)
            self.assertTrue(raw.strip(), path)
        att_status, _att_raw, _att_hdrs = _get_full(self.port, "/attestation")
        self.assertIn(att_status, (200, 404))
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

    def test_route_and_dashboard_share_v2_chrome(self):
        extra = {
            "/route": _get_full(self.port, "/route", extra_headers={"Accept": "text/html"})[1],
            "/dashboard": _get_full(self.port, "/dashboard")[1],
        }
        for path, html in extra.items():
            parsed = _parse(html)
            self.assertEqual([t for t, _h in parsed.nav_links], list(NAV_LABELS), path)
            self.assertNotIn("Listed on", html, path)
            self.assertNotIn("listed-on", html, path)
            self.assertIn("https://github.com/402signal/402signal", html, path)
            self.assertIn(">Transparency<", html, path)
            self.assertEqual(html.count("<h1"), 1, path)
            self.assertNotIn("\N{EM DASH}", html, path)

    def test_signal_flow_is_homepage_only(self):
        html = self.pages["/"]
        self.assertIn('<figure class="signal-flow"', html)
        self.assertEqual(html.count('<figure class="signal-flow"'), 1)
        self.assertIn("<figcaption", html)
        self.assertIn("DISCOVERY", html)
        self.assertIn("402SIGNAL", html)
        self.assertIn("YOUR AGENT", html)
        self.assertIn("COMMIT", html)
        self.assertIn("SIGN", html)
        self.assertIn("ANCHOR", html)
        self.assertNotIn('class="signal-flow" role="img"', html)
        self.assertNotIn('role="img" aria-label=', html)
        start = html.find('<figure class="signal-flow"')
        end = html.find("</figure>", start)
        self.assertGreater(end, start)
        self.assertNotIn('role="img"', html[start:end])
        for path in ("/how", "/transparency"):
            other = self.pages[path]
            self.assertNotIn('class="signal-flow"', other, path)
            self.assertNotIn("<figure class=\"signal-flow\"", other, path)

    def test_transparency_privacy_copy_and_no_customer_ui(self):
        html = self.transparency
        self.assertIn("Transparent history, not public requests", html)
        self.assertIn("It does not directly publish your wallet, raw request, or payment credentials.", html)
        self.assertNotIn("doesn’t reveal", html)
        self.assertNotIn("doesn't reveal", html)
        self.assertNotIn("does not reveal", html)
        self.assertNotIn("used car", html)
        self.assertNotIn("What did my agent rely on when it spent my money?", html)
        self.assertNotIn("<form", html)
        self.assertNotIn("PAYMENT-SIGNATURE", html)
        self.assertNotIn("customer-search", html)

    def test_seo_titles(self):
        self.assertIn("<title>402Signal: Find a paid API that works right now</title>", self.home)
        self.assertIn("<title>x402 API Catalog · 402Signal</title>", self.catalog)
        self.assertIn("<title>How 402Signal Works</title>", self.how)
        self.assertIn("<title>402Signal Developer API</title>", self.devs)
        self.assertIn("<title>402Signal Transparency. Verify the routing history</title>", self.transparency)

    def test_transparency_keeps_testnet_and_not_seller_truth(self):
        html = self.transparency
        self.assertIn("TestNet", html)
        self.assertIn("It is not a merchant payment.", html)
        self.assertIn("does not prove an endpoint", html)
        self.assertIn("Falcon does not make Base or Solana payments PQ-safe", html)
        self.assertIn("Routing never waits for blockchain confirmation.", html)
        self.assertIn("Later rewriting inconsistent with published checkpoints becomes detectable.", html)
        self.assertNotIn("See the check-first flow on the", html)
        self.assertNotIn("MainNet", html)
        self.assertNotIn('class="signal-flow"', html)


if __name__ == "__main__":
    unittest.main()
