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
from live402 import site_chrome


STATIC = Path(__file__).resolve().parent.parent / "live402" / "static"

NAV_LABELS = ("Home", "How it works", "Explore", "Developers", "Contact")
NAV_HREFS = ("/", "/how", "/catalog", "/developers", "/contact")
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
    "Catalogs are candidates, not truth.",
    "Fresh, not just listed",
    "Policy actually matters",
    "A miss is a valid answer",
    "Your wallet stays yours",
    "route or honest miss",
    "Trust the history, too.",
    "Integrate in two minutes.",
    "Build the decision your agent would make.",
    "The route is gold and synchronous.",
    "Try 402Signal",
    "Try it",
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
        cls.contact = _get_full(cls.port, "/contact")[1]
        cls.transparency = _get_full(cls.port, "/transparency")[1]
        cls.js = _read("app.js")
        cls.css = _read("styles.css")
        cls.pages = {
            "/": cls.home,
            "/catalog": cls.catalog,
            "/how": cls.how,
            "/developers": cls.devs,
            "/contact": cls.contact,
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
        self.assertIn("402Signal · Live x402 routing checks", html)
        self.assertIn(
            "402Signal searches x402 discovery sources, probes candidate endpoints, and applies caller constraints to observed payment options on Base, Solana, and Algorand.",
            html,
        )
        self.assertIn(
            "402Signal searches x402 discovery sources across Base, Solana, and Algorand, probes candidate endpoints immediately before spend, and applies the caller's constraints to the payment options returned by those endpoints.",
            html,
        )
        self.assertIn(
            "It returns a route only when a current observed option qualifies; otherwise it returns a typed miss.",
            html,
        )
        self.assertIn("402Signal does not hold the buyer's keys or execute the selected service's payment.", html)
        self.assertIn('class="discover-row"', html)
        self.assertNotIn("<summary>Discoverable via</summary>", html)
        self.assertIn("Base · Solana · Algorand", html)
        self.assertIn("Build a route request", html)
        self.assertIn("Developer docs", html)
        self.assertIn('href="/catalog"', html)
        self.assertIn('href="/developers"', html)
        self.assertIn("Current observation", html)
        self.assertIn("Constraint enforcement", html)
        self.assertIn("Explicit failure modes", html)
        self.assertIn("Execution boundary", html)
        self.assertIn("payment requirements observed during this request", html)
        self.assertIn("applied to those observed options before a winner is selected", html)
        self.assertIn("typed miss that reports why evaluation stopped", html)
        self.assertIn("caller retains custody of keys", html)
        self.assertIn("402Signal is currently listed in the following public directories and registries.", html)
        self.assertIn("These links confirm directory presence; they are not endorsements.", html)
        self.assertIn("POST /route · $0.01 USDC per routing check", html)
        self.assertIn("Search supported discovery sources", html)
        self.assertIn("LIVE CHECK", html)
        self.assertIn("Probe candidates and parse current HTTP 402 payment requirements", html)
        self.assertIn("Apply caller constraints and rank eligible observed candidates", html)
        self.assertIn("Return selected route and selected_payment, or a typed miss", html)

        css = Path("live402/static/styles.css").read_text(encoding="utf-8")
        self.assertIn("overflow-wrap: break-word", css)
        self.assertRegex(css, r"\.flow-box\s*\{[^}]*height:\s*auto")
        self.assertIn(".signal-row.flow-four", css)
        # Mobile stack must beat desktop .signal-row.flow-four (2-class) rule.
        self.assertRegex(
            css,
            r"@media \(max-width: 720px\)[\s\S]*?\.signal-row\.flow-four\s*\{[^}]*grid-template-columns:\s*1fr",
        )
        self.assertIn("PQ TRUST", html)
        self.assertIn("Commit route evidence to the PQ Trust log", html)
        self.assertIn("Algorand MainNet", html)
        self.assertIn("Awaiting checkpoint", html)
        self.assertIn('class="pq-badge"', html)
        self.assertIn('class="pq-chip"', html)
        self.assertNotIn("Signed checkpoints are periodically anchored to Algorand MainNet.", html)
        self.assertNotIn("periodically anchored to Algorand TestNet", html)
        self.assertNotIn("Currently Algorand TestNet", html)
        self.assertNotIn("Latest confirmed Tree", html)
        self.assertIn("Falcon anchors checkpoints, not merchant payments.", html)
        self.assertIn("Verifiable routing history", html)
        self.assertIn("PQ Trust is 402Signal's append-only transparency layer", html)
        self.assertIn("append-only Merkle log", html)
        self.assertIn('class="pq-trust"', html)
        self.assertNotIn("pq-testnet", html)
        self.assertIn("View verification details", html)
        self.assertIn("Algorand MainNet", html)
        self.assertIn('class="signal-flow"', html)
        self.assertIn('class="trust-rail"', html)
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
        self.assertNotIn(">Integrate<", html)
        self.assertIn(">Explore<", html)
        self.assertIn('href="/catalog">Explore<', html)
        self.assertNotIn(">Pulse<", html)
        self.assertNotIn('href="/pulse"', html)

    def test_exactly_one_h1_per_human_page(self):
        expected = {
            "/": "Find a paid API that works right now.",
            "/catalog": "Build a /route request",
            "/how": "How routing works",
            "/developers": "Developer API",
            "/contact": "Contact 402Signal",
            "/transparency": "Verify the transparency log",
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
            self.assertEqual(hrefs, list(NAV_HREFS), path)
            self.assertNotIn("Catalog", labels)
            self.assertNotIn("Try it", labels)
            self.assertIn("Home", labels)
            self.assertIn("Explore", labels)
            self.assertIn("Contact", labels)
            self.assertNotIn("GitHub", labels)
            self.assertNotIn("Transparency", labels)
            self.assertIn('class="mark"', html)
            self.assertIn('class="brand-name"', html)
            self.assertIn(">402Signal<", html)
            self.assertNotIn(">Product<", html)
            self.assertNotIn(">Pulse<", html)
            self.assertNotIn('class="sep"', html)
            self.assertNotIn(" | ", _parse(html).nav_links[0][0] if parsed.nav_links else "")

    def test_shared_chrome_cannot_drift(self):
        self.assertEqual(site_chrome.NAV, tuple(zip(NAV_HREFS, NAV_LABELS)))
        self.assertEqual(site_chrome.CONTACT_EMAIL, "ross@402signal.com")
        self.assertEqual(site_chrome.CONTACT_MAILTO, "mailto:ross@402signal.com")
        footer_hrefs = [href for href, _label, _ext in site_chrome.FOOTER]
        for path, html in self.pages.items():
            parsed = _parse(html)
            self.assertEqual(
                [href for _t, href in parsed.nav_links],
                [href for href, _label in site_chrome.NAV],
                path,
            )
            self.assertEqual(
                [text for text, _h in parsed.nav_links],
                [label for _href, label in site_chrome.NAV],
                path,
            )
            for href in footer_hrefs:
                self.assertIn('href="%s"' % href, html, path)
            self.assertIn(site_chrome.CONTACT_EMAIL, html, path)
            self.assertNotIn("402signal@gmail.com", html, path)
        dash = _get_full(self.port, "/dashboard")[1]
        route = _get_full(self.port, "/route", extra_headers={"Accept": "text/html"})[1]
        for path, html in (("/dashboard", dash), ("/route", route)):
            parsed = _parse(html)
            self.assertEqual([t for t, _h in parsed.nav_links], list(NAV_LABELS), path)
            self.assertIn("mailto:ross@402signal.com", html, path)

    def test_github_link_works(self):
        for path, html in self.pages.items():
            self.assertIn('href="https://github.com/402signalhq/402signal"', html, path)

    def test_catalog_page_search_uses_preview(self):
        html = self.catalog
        self.assertIn("Build a /route request", html)
        self.assertIn("The capability field accepts free text.", html)
        self.assertIn("The examples below are shortcuts, not a fixed list.", html)
        self.assertIn("What does your agent need?", html)
        self.assertIn("Describe any capability in plain English.", html)
        self.assertIn('placeholder="Describe a capability…"', html)
        self.assertIn("Example capabilities", html)
        self.assertIn("Discovery preview", html)
        self.assertIn("Search current discovery metadata and show any prior 402Signal observation on file.", html)
        self.assertIn("This does not probe the endpoint again.", html)
        self.assertIn("FREE · NOT A LIVE CHECK", html)
        self.assertIn("Discovery preview uses the capability and discovery network settings above.", html)
        self.assertIn("The route constraints below are included in the paid /route request and are not applied to preview results.", html)
        self.assertIn("POST /route performs the paid live check.", html)
        self.assertIn("This page builds the request but does not submit it.", html)
        self.assertNotIn("PQ", html)
        self.assertNotIn("Falcon", html)
        self.assertIn('id="need"', html)
        self.assertIn('id="search-form"', html)
        self.assertIn('id="search-btn"', html)
        self.assertIn(">Preview discovery<", html)
        self.assertNotIn("Generate live route request", html)
        self.assertIn(">Copy JSON<", html)
        self.assertIn(">Copy curl<", html)
        self.assertIn('href="/developers"', html)
        self.assertIn('id="policy-summary"', html)
        self.assertIn('data-need="web search"', html)
        self.assertIn('data-need="weather"', html)
        self.assertIn('data-need="token risk"', html)
        self.assertIn('data-need="LLM inference"', html)
        self.assertIn('data-need="wallet balance"', html)
        self.assertEqual(html.lower().count("free catalog search"), 0)
        self.assertIn("Build a 402Signal /route request", html)
        self.assertNotIn("window.ethereum", html)
        self.assertNotIn("Pay $0.01 on Base", html)
        status, raw, _hdrs = _get_full(self.port, "/preview?need=weather")
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertTrue(body.get("not_probed"))
        self.assertIn("hits", body)
        self.assertIn("/preview?need=", self.js)
        self.assertIn("previewUrl()", self.js)
        self.assertNotIn('fetch("/route"', self.js)

    def test_catalog_results_are_compact_catalog_only(self):
        js = self.js
        self.assertIn("result-row", js)
        self.assertIn("Listed price", js)
        self.assertIn("Schema listed", js)
        self.assertIn("DISCOVERY LISTING · ", js)
        self.assertIn("discovery matches", js)
        self.assertIn("matches returned by discovery", js)
        self.assertIn(" shown", js)
        self.assertIn("DISCOVERY LISTING", js)
        self.assertIn("PRIOR 402SIGNAL OBSERVATION", js)
        self.assertIn("No prior 402Signal observation", js)
        self.assertNotIn("LISTED · catalog claim", js)
        self.assertNotIn("LAST OBSERVED · 402Signal history", js)
        self.assertNotIn("DISCOVERED · catalog listing", js)
        self.assertNotIn("OBSERVED · prior 402Signal check", js)
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
        self.assertIn(" observations", js)
        self.assertIn("obs.n_7d", js)
        self.assertNotIn("observations in 7d", js)
        self.assertNotIn(" in 7d · ", js)
        self.assertNotIn("7d reliability", js)
        self.assertIn("No catalog matches found. Try a broader capability.", js)
        self.assertIn("Catalog data is refreshing. Try again shortly.", js)
        self.assertNotIn("MIN_RELIABILITY_N", js)
        self.assertNotIn("claimed-vs-observed", self.catalog)
        self.assertNotIn("Catalog claims", self.catalog)
        self.assertNotIn("verified provider", (self.catalog + js).lower())
        self.assertNotIn("trusted merchant", (self.catalog + js).lower())
        self.assertNotIn("quality verified", (self.catalog + js).lower())
        self.assertNotIn("healthy", js)
        self.assertNotIn("Executable Now Rate", js)
        self.assertNotIn("ENR", js)

    def test_catalog_policy_builder_maps_supported_fields_only(self):
        html = self.catalog
        js = self.js
        self.assertIn('id="max-price"', html)
        self.assertIn('id="require-invocable"', html)
        self.assertIn('id="min-observations"', html)
        self.assertIn('data-network="any"', html)
        self.assertIn('data-network="base"', html)
        self.assertIn('data-network="solana"', html)
        self.assertIn('data-network="algorand"', html)
        self.assertIn('data-objective="best"', html)
        self.assertIn('data-objective="cheapest"', html)
        self.assertIn('data-objective="fastest"', html)
        self.assertIn('data-objective="most_reliable"', html)
        self.assertIn('data-prefer="solana"', html)
        self.assertIn('id="max-total-cost"', html)
        self.assertIn('id="max-latency"', html)
        self.assertIn('data-depth="standard"', html)
        self.assertIn('data-depth="thorough"', html)
        self.assertIn('id="route-json"', html)
        self.assertNotIn('id="copy-route"', html)
        self.assertIn("Required network", html)
        self.assertIn("Limits discovery and paid selection to the selected network.", html)
        self.assertIn("Leave as Any to search all supported networks.", html)
        self.assertIn("No preference", html)
        self.assertIn("Used for ranking only. It does not restrict the eligible networks.", html)
        self.assertIn("currently probed eligible candidates", html)
        self.assertIn("This is not settlement latency.", html)
        self.assertNotIn("Hard policy lock", html)
        self.assertNotIn("Network lock", html)
        self.assertNotIn("Weak preference", html)
        self.assertIn("POST /route will search for ", js)
        self.assertIn("require a current ", js)
        self.assertIn("require invocation metadata", js)
        self.assertIn("rank the eligible probed candidates by known merchant price", js)
        self.assertIn("body.networks", js)
        self.assertIn("body.max_price_usd", js)
        self.assertIn("body.require_invocable", js)
        self.assertIn("body.min_observations", js)
        self.assertIn("body.objective", js)
        self.assertIn("body.prefer_network", js)
        self.assertIn("body.max_total_cost_usd", js)
        self.assertIn("body.max_latency_ms", js)
        self.assertIn('body.search_depth = "thorough"', js)
        self.assertIn("&networks=", js)
        self.assertIn("&prefer_network=", js)
        self.assertNotIn("body.max_service_latency_ms", js)
        self.assertNotIn("body.max_settlement_latency_ms", js)
        self.assertNotIn("body.max_candidates_to_probe", js)
        self.assertNotIn("body.min_observed_success", js)
        self.assertNotIn("body.min_reputation_score", js)
        self.assertNotIn("lowest_total_cost", html)
        self.assertNotIn("fastest_settlement", html)
        self.assertNotIn("healthy", html.lower())
        self.assertNotIn("recommended", html.lower())
        self.assertNotIn("live now", html.lower())
        self.assertNotIn("verified now", html.lower())
        self.assertNotIn("window.ethereum", js)
        self.assertNotIn("Pay $0.01 on Base", html)

    def test_how_page_renders(self):
        html = self.how
        self.assertIn("How routing works", html)
        self.assertIn("Discovery sources publish metadata about x402 services.", html)
        self.assertIn("A paid POST /route check observes the endpoint's current HTTP 402 payment requirements", html)
        self.assertIn(">DISCOVER<", html)
        self.assertIn(">PROBE<", html)
        self.assertIn(">EVALUATE<", html)
        self.assertIn(">SELECT<", html)
        self.assertIn("Search supported discovery sources", html)
        self.assertIn("parse the current HTTP 402 payment requirements", html)
        self.assertIn("Apply the caller's constraints to the payment options returned during this request.", html)
        self.assertIn("typed miss that reports why evaluation stopped", html)
        self.assertIn("Execution boundary", html)
        self.assertIn("Retains custody of keys, signing, the selected service's payment, and execution of the seller request.", html)
        self.assertNotIn("See the flow on the", html)
        self.assertNotIn('class="signal-flow"', html)
        self.assertNotIn("Trust path", html)
        self.assertIn("View verification details", html)
        self.assertIn('href="/transparency"', html)
        self.assertIn("How 402Signal routing works", html)
        self.assertNotIn("Where it fits", html)
        self.assertNotIn("HEALTHY", html)
        self.assertNotIn("honest", html.lower())
        self.assertIn(
            "Routing evidence is committed to the PQ Trust log. Production identity "
            "is Algorand MainNet. Falcon anchors checkpoints, not merchant payments.",
            html,
        )
        self.assertNotIn("periodically anchored to Algorand MainNet", html)
        self.assertNotIn("periodically anchored to Algorand TestNet", html)
        self.assertNotIn("Currently Algorand TestNet", html)
        self.assertIn("Routing does not wait for confirmation.", html)
        self.assertIn("does not require a durable signed leaf unless require_transparency is set", html)
        self.assertNotIn("may later be anchored", html)

    def test_developers_page_renders(self):
        html = self.devs
        self.assertIn("Developer API", html)
        self.assertIn("Use 402Signal over HTTP or MCP.", html)
        self.assertIn("An unpaid call returns HTTP 402.", html)
        self.assertIn("POST /route → unpaid HTTP 402 → pay $0.01 USDC → retry → route or typed miss.", html)
        self.assertIn("Common recipes", html)
        self.assertIn("Require Solana", html)
        self.assertIn("Maximum price + invocation schema", html)
        self.assertIn("Rank eligible candidates by merchant price", html)
        self.assertIn('{"need":"weather","networks":["solana"]}', html)
        self.assertIn('{"need":"weather","max_price_usd":0.05,"require_invocable":true}', html)
        self.assertIn('{"need":"weather","objective":"cheapest"}', html)
        self.assertIn("constraints_unmet", html)
        self.assertIn("probe_limit_reached", html)
        self.assertIn("Example response · selected route", html)
        self.assertIn("Example response · typed miss", html)
        self.assertIn("synthetic examples", html)
        self.assertIn("not live evidence", html)
        self.assertIn("discovered_count", html)
        self.assertIn("probed_count", html)
        self.assertIn("unprobed_count", html)
        self.assertIn("evaluation_complete", html)
        self.assertIn("normalized_usd", html)
        self.assertIn("402Signal does not hold the buyer's keys", html)
        self.assertIn("<details", html)
        self.assertIn(">HTTP<", html)
        self.assertIn(">MCP<", html)
        self.assertIn("<code>POST /route</code>", html)
        self.assertIn("id=\"copy-curl\"", html)
        self.assertIn("curl -sS -D - https://402signal.com/route", html)
        self.assertIn("<code>https://402signal.com/mcp</code>", html)
        self.assertIn("402Signal API · HTTP and MCP", html)
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
        self.assertIn("PQ Trust", html)
        self.assertNotIn("post-quantum", html.lower())
        self.assertNotIn("algo_bonus", html)
        self.assertNotIn("TestNet Falcon broadcast is off unless explicitly enabled.", html)
        self.assertIn("402Signal maintains a public append-only PQ Trust log", html)
        self.assertIn("Production identity is Algorand MainNet", html)
        self.assertIn("Falcon anchors checkpoints, not merchant payments.", html)
        self.assertNotIn("Historical TestNet archive", html)
        self.assertNotIn("Currently Algorand TestNet", html)
        self.assertNotIn("periodically anchored to Algorand TestNet", html)
        self.assertIn("The human-readable transparency view is available at", html)
        self.assertIn('href="/transparency"', html)
        self.assertIn("does not require a durable signed leaf unless", html)
        self.assertIn("require_transparency", html)

    def test_developers_example_fields_are_in_openapi_route_schema(self):
        spec = json.loads(_get_full(self.port, "/openapi.json")[1])
        live_props = (
            spec["paths"]["/route"]["post"]["responses"]["200"]["content"]
            ["application/json"]["schema"]["properties"]
        )
        pay_props = live_props["selected_payment"]["properties"]
        compared_props = live_props["compared"]["items"]["properties"]
        for key in (
            "live",
            "payable",
            "invocable",
            "url",
            "probed_at",
            "selected_payment",
            "compared",
            "discovered_count",
            "probed_count",
            "unprobed_count",
            "evaluation_complete",
            "stop_reason",
            "pq_trust",
            "miss_reason",
            "unmet_constraints",
        ):
            self.assertIn(key, live_props, key)
            self.assertIn(key, self.devs, key)
        for key in ("network", "amount_atomic", "normalized_usd", "display_amount"):
            self.assertIn(key, pay_props, key)
        self.assertIn("selected", compared_props)
        self.assertIn("Example response", self.devs)
        self.assertIn("synthetic", self.devs.lower())

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
                self.assertIn("Public directories", html, path)
                self.assertIn('class="discover-row"', html, path)
                self.assertNotIn("<summary>Discoverable via</summary>", html, path)
                self.assertNotIn("Discoverable via", html, path)
                self.assertNotIn("Listed on", html, path)
                self.assertIn("402Signal is currently listed in the following public directories and registries.", html, path)
                self.assertIn("These links confirm directory presence; they are not endorsements.", html, path)
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
            self.assertIn(">Contact<", html, path)
            self.assertIn("https://x.com/402Signal", html, path)
            self.assertIn('href="/openapi.json"', html, path)
            self.assertIn(">OpenAPI<", html, path)
            self.assertIn('href="/mcp.json"', html, path)
            self.assertIn(">MCP<", html, path)
            self.assertIn('href="/transparency"', html, path)
            self.assertIn(">Transparency<", html, path)
            self.assertIn('href="/catalog"', html, path)
            self.assertIn("https://github.com/402signalhq/402signal", html, path)

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
        contact = _read("contact.html")
        self.assertNotIn("<form", contact)
        self.assertNotIn("<script", contact)
        self.assertIn("Do not send private keys, mnemonics, payment credentials, or other secrets.", contact)

    def test_contact_page(self):
        status, html, hdrs = _get_full(self.port, "/contact")
        self.assertEqual(status, 200)
        self.assertIn("text/html", hdrs.get("content-type", ""))
        self.assertEqual(
            hdrs.get("content-security-policy"),
            "default-src 'none'; script-src 'self'; "
            "connect-src 'self'; "
            "style-src 'self'; img-src 'self' data:; base-uri 'self'; "
            "frame-ancestors 'none'",
        )
        self.assertIn("<title>Contact 402Signal</title>", html)
        self.assertEqual(_parse(html).h1, ["Contact 402Signal"])
        self.assertIn("Questions, integration help, feedback, and bug reports are welcome.", html)
        self.assertIn("mailto:ross@402signal.com", html)
        self.assertIn("https://x.com/402Signal", html)
        self.assertIn("https://github.com/402signalhq/402signal", html)
        self.assertIn("Send security-sensitive reports by email, not as public posts.", html)
        self.assertNotIn("<form", html)
        self.assertNotIn("402signal@gmail.com", html)
        parsed = _parse(html)
        self.assertEqual([t for t, _h in parsed.nav_links], list(NAV_LABELS))

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
            "live402/static/contact.html",
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
        for path in ("/", "/catalog", "/how", "/developers", "/contact", "/transparency"):
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
            self.assertIn("https://github.com/402signalhq/402signal", html, path)
            self.assertIn(">Transparency<", html, path)
            self.assertEqual(html.count("<h1"), 1, path)
            self.assertNotIn("\N{EM DASH}", html, path)

    def test_signal_flow_is_homepage_only(self):
        html = self.pages["/"]
        self.assertIn('<figure class="signal-flow"', html)
        self.assertEqual(html.count('<figure class="signal-flow"'), 1)
        self.assertIn("<figcaption", html)
        self.assertIn("DISCOVERY", html)
        self.assertIn("LIVE CHECK", html)
        self.assertIn("POLICY", html)
        self.assertIn("RESULT", html)
        self.assertIn("PQ TRUST", html)
        self.assertNotIn("YOUR AGENT", html)
        self.assertNotIn("What catalogs claim", html)
        self.assertNotIn("Check it now", html)
        self.assertNotIn("Decide whether to spend", html)
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
        self.assertIn("Public transparency commitments do not expose raw needs, wallets, payment signatures, or seller response bodies.", html)
        self.assertIn("What is published?", html)
        self.assertIn("This page publishes 402Signal infrastructure commitments", html)
        self.assertIn("What this proves / does not prove", html)
        self.assertIn("Detectability is not a claim that the log cannot be rewritten", html)
        self.assertNotIn("doesn’t reveal", html)
        self.assertNotIn("doesn't reveal", html)
        self.assertNotIn("does not reveal", html)
        self.assertNotIn("used car", html)
        self.assertNotIn("What did my agent rely on when it spent my money?", html)
        self.assertNotIn("<form", html)
        self.assertNotIn("PAYMENT-SIGNATURE", html)
        self.assertNotIn("customer-search", html)

    def test_seo_titles(self):
        self.assertIn("<title>402Signal · Live x402 routing checks</title>", self.home)
        self.assertIn("<title>Build a 402Signal /route request</title>", self.catalog)
        self.assertIn("<title>How 402Signal routing works</title>", self.how)
        self.assertIn("<title>402Signal API · HTTP and MCP</title>", self.devs)
        self.assertIn("<title>Contact 402Signal</title>", self.contact)
        self.assertIn("<title>402Signal transparency log</title>", self.transparency)

    def test_transparency_mainnet_and_not_seller_truth(self):
        html = self.transparency
        self.assertIn("Algorand MainNet", html)
        self.assertIn("Algorand MainNet log · awaiting first confirmed checkpoint", html)
        self.assertIn("Production log identity is Algorand MainNet", html)
        self.assertIn("Awaiting first confirmed MainNet checkpoint", html)
        self.assertNotIn("Signed checkpoints are periodically anchored to Algorand MainNet", html)
        self.assertIn("Historical TestNet archive", html)
        self.assertNotIn("—", html)
        self.assertIn("It is not a merchant payment.", html)
        self.assertIn("does not report whether a seller endpoint described its service accurately", html)
        self.assertIn("native Falcon-1024 post-quantum authorization", html)
        self.assertIn("does not make Base or Solana merchant payments post-quantum secure", html)
        self.assertNotIn("PQ-safe", html)
        self.assertNotIn("quantum-proof", html.lower())
        self.assertIn("Authorization · Falcon-1024 · f1 as native Algorand PQ tx auth for the checkpoint", html)
        self.assertIn("Routing does not wait for confirmation.", html)
        self.assertIn("does not require a durable signed leaf unless require_transparency is set", html)
        self.assertIn("Later rewriting inconsistent with published checkpoints becomes detectable.", html)
        self.assertNotIn("See the check-first flow on the", html)
        self.assertNotIn("Currently Algorand TestNet", html)
        self.assertNotIn('class="signal-flow"', html)

    def test_catalog_chip_groups_start_with_exactly_one_selected(self):
        html = self.catalog
        for group_id in ("network-chips", "prefer-chips", "objective-chips", "depth-chips"):
            match = re.search(r'id="%s"[^>]*>(.*?)</div>' % group_id, html, re.S)
            self.assertIsNotNone(match, group_id)
            block = match.group(1)
            self.assertEqual(block.count('class="chip active"'), 1, group_id)
            self.assertEqual(block.count('aria-pressed="true"'), 1, group_id)
            self.assertEqual(block.count('aria-pressed="false"'), block.count('class="chip"'), group_id)

    def test_catalog_json_mapping_and_empty_state(self):
        html = self.catalog
        js = self.js
        self.assertIn('body.networks = [policy.network]', js)
        self.assertNotIn("body.network =", js)
        self.assertIn('body.prefer_network = policy.preferNetwork', js)
        self.assertIn('if (policy.objective !== "best"', js)
        self.assertIn('body.search_depth = "thorough"', js)
        self.assertIn('policy.searchDepth === "thorough"', js)
        self.assertNotIn('fetch("/route"', js)
        self.assertIn("Enter a capability above to generate the request body.", html)
        self.assertIn("Enter a capability above to generate the request body.", js)
        self.assertIn(">Enter a capability above to generate the request body.</pre>", html)
        self.assertNotIn(">{}</pre>", html)
        self.assertIn("copyRouteJsonBtn.disabled = !ready", js)
        self.assertIn("copyRouteCurlBtn.disabled = !ready", js)
        self.assertIn(">Default ranking<", html)
        self.assertIn(">Lowest price<", html)
        self.assertIn(">Lowest probe latency<", html)
        self.assertIn(">Observed reliability<", html)
        self.assertIn(">No preference<", html)
        self.assertIn("Minimum prior observations", html)
        self.assertIn("Require invocation schema", html)
        self.assertIn("body.require_invocable", js)
        self.assertIn(">Generated request<", html)
        self.assertNotIn("Request documentation", html)
        self.assertNotIn(">Generate<", html)
        self.assertNotIn("Generate live", html)
        self.assertIn("c.classList.toggle(\"active\", on)", js)
        self.assertIn('c.setAttribute("aria-pressed", on ? "true" : "false")', js)

    def test_selected_vs_focus_are_distinct(self):
        css = self.css
        self.assertIn(".chip.active", css)
        self.assertIn('.chip[aria-pressed="true"]', css)
        self.assertIn(".chip:focus-visible", css)
        self.assertIn(".chip:focus { outline: none; }", css)
        self.assertNotIn(".chip:hover, .chip:focus { border-color: var(--accent);", css)
        focus_block = css.split(".chip:focus-visible", 1)[1].split("}", 1)[0]
        self.assertNotIn("var(--accent)", focus_block)
        self.assertNotIn("var(--fox)", focus_block)

    def test_human_html_references_versioned_assets(self):
        from live402 import asset_version

        ver = asset_version.asset_version()
        self.assertTrue(ver)
        self.assertNotIn("/", ver)
        self.assertNotIn(":", ver)
        for path, html in self.pages.items():
            self.assertIn("/styles.css?v=%s" % ver, html, path)
            self.assertNotIn('href="/styles.css"', html, path)
            self.assertNotIn("FLY_IMAGE_REF", html, path)
        self.assertIn("/app.js?v=%s" % ver, self.catalog)
        self.assertIn("/app.js?v=%s" % ver, self.devs)
        self.assertIn("/transparency.js?v=%s" % ver, self.transparency)
        dash = _get_full(self.port, "/dashboard")[1]
        route = _get_full(self.port, "/route", extra_headers={"Accept": "text/html"})[1]
        self.assertIn("/dashboard.js?v=%s" % ver, dash)
        self.assertIn("/styles.css?v=%s" % ver, route)
        for name in ("index.html", "catalog.html", "how.html", "developers.html", "contact.html"):
            source = _read(name)
            self.assertIn('href="/styles.css"', source, name)
            self.assertNotIn("?v=", source, name)

    def test_html_revalidates_and_fingerprinted_assets_can_long_cache(self):
        from live402 import asset_version

        ver = asset_version.asset_version()
        _st, _raw, home_hdrs = _get_full(self.port, "/")
        self.assertEqual(home_hdrs.get("cache-control"), asset_version.HTML_REVALIDATE)
        _st, _raw, cat_hdrs = _get_full(self.port, "/catalog")
        self.assertEqual(cat_hdrs.get("cache-control"), asset_version.HTML_REVALIDATE)
        _st, _raw, how_hdrs = _get_full(self.port, "/how")
        self.assertEqual(how_hdrs.get("cache-control"), asset_version.HTML_REVALIDATE)
        _st, _raw, tr_hdrs = _get_full(self.port, "/transparency")
        self.assertEqual(tr_hdrs.get("cache-control"), "no-store")
        _st, css, css_hdrs = _get_full(self.port, "/styles.css?v=%s" % ver)
        self.assertEqual(_st, 200)
        self.assertIn("text/css", css_hdrs.get("content-type", ""))
        self.assertEqual(css_hdrs.get("cache-control"), asset_version.ASSET_LONG_CACHE)
        _st, js, js_hdrs = _get_full(self.port, "/app.js?v=%s" % ver)
        self.assertEqual(_st, 200)
        self.assertNotIn("text/html", js_hdrs.get("content-type", ""))
        self.assertEqual(js_hdrs.get("cache-control"), asset_version.ASSET_LONG_CACHE)
        _st, _raw, bare_hdrs = _get_full(self.port, "/styles.css")
        self.assertEqual(bare_hdrs.get("cache-control"), asset_version.HTML_REVALIDATE)
        health_st, health_raw, _hdrs = _get_full(self.port, "/health")
        self.assertEqual(health_st, 200)
        self.assertEqual(json.loads(health_raw), {"ok": True})
        self.assertNotIn(ver, health_raw)
        for sneak in (
            "/styles.css/../server.py",
            "/app.js/../../README.md",
            "/dashboard.js/%2e%2e/asset_version.py",
        ):
            sneak_st, sneak_raw, sneak_hdrs = _get_full(self.port, sneak)
            self.assertEqual(sneak_st, 404, sneak)
            self.assertIn("json", sneak_hdrs.get("content-type", ""), sneak)
            self.assertNotIn("def ", sneak_raw, sneak)

    def test_post_quantum_wording_precise_not_saturated(self):
        banned = (
            "FN-DSA",
            "FIPS 206",
            "quantum-proof",
            "fully quantum-safe",
            "PQ-safe",
            "merchant payments PQ-safe",
        )
        self.assertIn("Falcon anchors checkpoints, not merchant payments.", self.home)
        self.assertIn("post-quantum", self.transparency.lower())
        self.assertIn("Falcon anchors checkpoints, not merchant payments.", self.how)
        self.assertNotIn("post-quantum", self.home.lower())
        self.assertNotIn("post-quantum", self.how.lower())
        self.assertNotIn("post-quantum", self.devs.lower())
        self.assertNotIn("post-quantum", self.catalog.lower())
        self.assertNotIn("post-quantum", self.contact.lower())
        for path, html in self.pages.items():
            for phrase in banned:
                self.assertNotIn(phrase, html, f"{path}: {phrase}")
            self.assertNotIn("Circle", html, path)
            self.assertNotIn(">Try 402Signal<", html, path)
            self.assertNotIn(">Try it<", html, path)
            labels = [text for text, _href in _parse(html).nav_links]
            self.assertIn("Explore", labels, path)
            self.assertNotIn("Try", labels, path)


if __name__ == "__main__":
    unittest.main()
