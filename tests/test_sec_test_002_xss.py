"""SEC-TEST-002: catalog/seller-string XSS must not break out of HTML.

Fixtures in tests/fixtures/sec_test_002_xss/ put <script>, javascript:, and
onerror= in seller/catalog description and schema fields. Dashboard, catalog,
and transparency HTML paths that include those strings must emit escaped
markup, not a raw tag/handler/javascript: href. CSP is not relaxed.

Tests only. No Fly. No CSP change.
"""

from __future__ import annotations

import html as html_mod
import json
import os
import re
import subprocess
import threading
import unittest
from html.parser import HTMLParser
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LIVE402_FIXTURE", "1")
os.environ.pop("LOCAL_FREE", None)

from live402 import pulse, site_chrome
from live402.pq import transparency as pq_view
from live402.server import CSP, Handler

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "sec_test_002_xss" / "catalog_items.json"
DASH_JS = ROOT / "live402" / "static" / "dashboard.js"
APP_JS = ROOT / "live402" / "static" / "app.js"
CATALOG_HTML = ROOT / "live402" / "static" / "catalog.html"

ALLOWED_SCRIPT_SRC = re.compile(
    r'<script src="/(?:app|dashboard|transparency)\.js(?:\?v=[^"]*)?"(?: defer)?></script>'
)
JS_HREF = re.compile(r"""\bhref\s*=\s*(['"])\s*javascript:""", re.I)
SCRIPT_TAG = re.compile(r"<script(?=[\s>])", re.I)
ALLOWED_SCRIPT_SRC_PATHS = frozenset({"/app.js", "/dashboard.js", "/transparency.js"})


class _BreakoutParser(HTMLParser):
    """Collect raw script tags and event-handler attributes (true HTML breakout)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts: list[tuple[str, list[tuple[str, str | None]]]] = []
        self.handlers: list[tuple[str, str, str | None]] = []
        self.js_hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts.append((tag, list(attrs)))
        for name, value in attrs:
            low = (name or "").lower()
            if low.startswith("on"):
                self.handlers.append((tag, low, value))
            if low in {"href", "src", "action", "formaction", "xlink:href"}:
                raw = (value or "").strip()
                if raw.lower().startswith("javascript:"):
                    self.js_hrefs.append(raw)


def _load_items() -> list[dict]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return list(payload.get("resources") or [])


def _walk_seller_strings(obj, out: list[str]) -> None:
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key in {"description", "serviceName", "toolName"} and isinstance(val, str):
                out.append(val)
            _walk_seller_strings(val, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_seller_strings(item, out)


def _seller_strings(items: list[dict] | None = None) -> list[str]:
    found: list[str] = []
    for item in items or _load_items():
        _walk_seller_strings(item, found)
    # Preserve order, drop empties / duplicates.
    seen: set[str] = set()
    out: list[str] = []
    for raw in found:
        if raw and raw not in seen:
            seen.add(raw)
            out.append(raw)
    return out


def _pulse_payload_from_fixtures() -> dict:
    """Pulse snapshot whose rendered samples include fixture seller strings."""
    samples = []
    for item in _load_items():
        desc = str(item.get("description") or "")
        name = str(item.get("serviceName") or desc)
        samples.append(
            {
                "need": desc,
                "label": name,
                "url": str(item.get("url") or ""),
                "price": desc,
            }
        )
        for field in _seller_strings([item]):
            samples.append(
                {
                    "need": field,
                    "label": field,
                    "url": str(item.get("url") or ""),
                    "price": field,
                }
            )
    host = next((s for s in _seller_strings() if "<script>" in s), "<script>alert(1)</script>")
    return {
        "updated_at": host,
        "chains": {
            "base": {
                "source": {"ok": True, "host": host},
                "samples": samples,
            },
            "solana": {"source": {"ok": True, "host": "catalog"}, "samples": []},
            "algorand": {"source": {"ok": True, "host": "catalog"}, "samples": []},
        },
    }


def _strip_allowed_scripts(html: str) -> str:
    return ALLOWED_SCRIPT_SRC.sub("", html)


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


def _get_full(port, path):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    res = conn.getresponse()
    raw = res.read()
    hdrs = {k.lower(): v for k, v in res.getheaders()}
    conn.close()
    return res.status, raw.decode("utf-8"), hdrs


def _column_html_js(chain: str, data: dict) -> str:
    """Run live402/static/dashboard.js columnHTML against untrusted samples.

    Slice out poll()/setInterval so node does not hang on the live refresh loop.
    """
    js = r"""
const fs = require("fs");
const src = fs.readFileSync(process.env.DASH_JS, "utf8");
const start = src.indexOf("var LABELS");
const end = src.indexOf("function render(");
if (start < 0 || end < 0 || end <= start) {
  throw new Error("dashboard.js columnHTML slice not found");
}
const slice = src.slice(start, end);
const fn = new Function("chain", "data", slice + "\nreturn columnHTML(chain, data);");
process.stdout.write(fn(process.env.DASH_CHAIN, JSON.parse(process.env.DASH_DATA)));
"""
    env = os.environ.copy()
    env["DASH_JS"] = str(DASH_JS)
    env["DASH_DATA"] = json.dumps(data)
    env["DASH_CHAIN"] = chain
    proc = subprocess.run(
        ["node", "-e", js],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("dashboard.js columnHTML failed: %s" % (proc.stderr or proc.stdout))
    return proc.stdout


class SecTest002XssTests(unittest.TestCase):
    def setUp(self):
        self.items = _load_items()
        self.strings = _seller_strings(self.items)
        self.assertGreaterEqual(len(self.items), 3)
        self.assertTrue(any("<script>" in s for s in self.strings))
        self.assertTrue(any("javascript:" in s for s in self.strings))
        self.assertTrue(any("onerror=" in s for s in self.strings))

    def _assert_escaped_not_breakout(self, rendered: str, *, allow_js_text: bool = True):
        """Seller strings may appear escaped; they must not be a raw breakout."""
        stripped = _strip_allowed_scripts(rendered)
        self.assertNotRegex(stripped, SCRIPT_TAG, "raw <script> breakout")
        self.assertNotRegex(stripped, JS_HREF, "javascript: href")
        self.assertNotIn("<script>", stripped)
        self.assertNotIn("<script ", stripped)
        # Attribute-breakout payload must not close a quoted attr then open a tag.
        self.assertNotIn('"><img', stripped)

        parsed = _BreakoutParser()
        parsed.feed(rendered)
        unexpected = []
        for _tag, attrs in parsed.scripts:
            src = ""
            for name, value in attrs:
                if name == "src" and value:
                    src = value.split("?", 1)[0]
                    break
            if src not in ALLOWED_SCRIPT_SRC_PATHS:
                unexpected.append(attrs)
        self.assertEqual(unexpected, [], "unexpected <script> tags")
        self.assertEqual(parsed.handlers, [], "event-handler attribute breakout")
        self.assertEqual(parsed.js_hrefs, [], "javascript: URL breakout")

        for raw in self.strings:
            if "<" in raw or ">" in raw or '"' in raw:
                escaped = html_mod.escape(raw, quote=True)
                self.assertIn(
                    escaped,
                    rendered,
                    "expected escaped seller/catalog string in HTML: %r" % raw,
                )
                if "<script>" in raw:
                    self.assertNotIn("<script>", stripped)
                if "<img" in raw.lower():
                    self.assertNotIn("<img ", stripped.lower())
                    self.assertNotIn("<img>", stripped.lower())
            if raw.startswith("javascript:") or "javascript:" in raw:
                if not allow_js_text:
                    self.assertNotIn("javascript:", rendered.lower())

    def test_fixtures_cover_description_and_schema_fields(self):
        blob = FIXTURE.read_text(encoding="utf-8")
        self.assertIn("SEC-TEST-002", Path(__file__).read_text(encoding="utf-8"))
        descriptions = [str(i.get("description") or "") for i in self.items]
        self.assertTrue(any("<script>" in d for d in descriptions))
        self.assertTrue(any("javascript:" in d for d in descriptions))
        self.assertTrue(any("onerror=" in d for d in descriptions))
        schema_descs = [
            s
            for s in self.strings
            if s is not descriptions[0] and ("<script>" in s or "onerror=" in s or "javascript:" in s)
        ]
        self.assertTrue(schema_descs, "schema field descriptions must carry payloads")
        self.assertIn('"description": "<script>alert(1)</script> city name"', blob)
        self.assertIn('"description": "javascript:alert(1) query"', blob)
        self.assertIn('"description": "<img src=x onerror=alert(1)> query"', blob)

    def test_dashboard_html_escapes_seller_strings(self):
        html = pulse.dashboard_html(_pulse_payload_from_fixtures())
        self._assert_escaped_not_breakout(html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("onerror=alert(1)", html)
        self.assertIn("&lt;img", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        # javascript: listing URL must not become an href.
        self.assertIsNone(pulse._https_href("javascript:alert(1)"))
        self.assertNotRegex(html, JS_HREF)

    def test_dashboard_js_column_html_escapes_seller_strings(self):
        payload = _pulse_payload_from_fixtures()
        rendered = _column_html_js("base", payload["chains"]["base"])
        self._assert_escaped_not_breakout(rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertNotRegex(rendered, JS_HREF)
        src = DASH_JS.read_text(encoding="utf-8")
        self.assertIn("function esc(s)", src)
        self.assertIn("esc(need)", src)
        self.assertIn("esc(price)", src)
        self.assertIn("esc(homeHref(s))", src)
        self.assertIn("innerHTML = columnHTML", src)

    def test_catalog_html_path_escapes_seller_strings(self):
        # Catalog page is static + textContent. If those seller strings are
        # interpolated into HTML (chrome / a result row), they must escape.
        fragments = [
            site_chrome.listed_on_html(title=s, note=s) for s in self.strings
        ]
        fragments.append(
            "<article class=\"result-row\">%s</article>"
            % "".join(
                "<p class=\"result-name\">%s</p><p class=\"result-url\">%s</p>"
                % (site_chrome.esc(s), site_chrome.esc(s))
                for s in self.strings
            )
        )
        rendered = "\n".join(fragments)
        self._assert_escaped_not_breakout(rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<script>alert(1)</script>", rendered)

        app = APP_JS.read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", app)
        self.assertIn("name.textContent = safeLabel(hit)", app)
        self.assertIn("urlP.textContent = String(hit.url)", app)
        self.assertIn("span.textContent = value", app)
        catalog = CATALOG_HTML.read_text(encoding="utf-8")
        self.assertNotIn("<script>alert", catalog)
        self.assertNotIn("onerror=", catalog)
        self.assertNotIn("javascript:", catalog.lower())

    def test_transparency_html_path_escapes_seller_strings(self):
        chunks = [
            pq_view._chrome_head(s, s, "https://402signal.com/transparency")
            for s in self.strings
        ]
        chunks.append(
            "<p>%s</p>\n" % pq_view.esc(self.strings[0])
        )
        rendered = "\n".join(chunks)
        self._assert_escaped_not_breakout(rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<script>alert(1)</script>", rendered)
        # Live transparency page must keep the same escape helper.
        self.assertIn("html_mod.escape", Path(pq_view.__file__).read_text(encoding="utf-8"))

    def test_https_href_rejects_javascript_scheme(self):
        self.assertIsNone(pulse._https_href("javascript:alert(1)"))
        self.assertIsNone(pulse._https_href("javascript:alert(document.domain)"))
        self.assertIsNone(pulse._https_href("JAVASCRIPT:alert(1)"))
        ok = pulse._https_href("https://fixture.402signal.local/xss-onerror")
        self.assertEqual(ok, "https://fixture.402signal.local/xss-onerror")


class SecTest002HttpPathsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.port = _serve()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _assert_csp_unweakened(self, hdrs: dict, path: str):
        csp = hdrs.get("content-security-policy") or ""
        self.assertEqual(csp, CSP, path)
        self.assertIn("script-src 'self'", csp)
        script_src = csp.split("script-src")[1].split(";")[0].strip()
        self.assertEqual(script_src, "'self'", path)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertNotIn("cdn.", csp)
        connect = csp.split("connect-src")[1].split(";")[0].strip()
        self.assertEqual(connect, "'self'", path)

    def test_dashboard_http_escapes_fixture_strings_same_csp(self):
        payload = _pulse_payload_from_fixtures()
        with patch("live402.pulse.get_pulse", return_value=payload):
            status, html, hdrs = _get_full(self.port, "/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("text/html", hdrs.get("content-type", ""))
        self._assert_csp_unweakened(hdrs, "/dashboard")
        stripped = _strip_allowed_scripts(html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotRegex(stripped, SCRIPT_TAG)
        self.assertNotRegex(html, JS_HREF)
        parsed = _BreakoutParser()
        parsed.feed(html)
        self.assertEqual(parsed.handlers, [])
        self.assertEqual(parsed.js_hrefs, [])
        self.assertIn("&lt;script&gt;", html)

    def test_catalog_and_transparency_http_keep_csp(self):
        for path in ("/catalog", "/transparency"):
            status, html, hdrs = _get_full(self.port, path)
            self.assertEqual(status, 200, path)
            self.assertIn("text/html", hdrs.get("content-type", ""), path)
            self._assert_csp_unweakened(hdrs, path)
            stripped = _strip_allowed_scripts(html)
            self.assertNotRegex(stripped, SCRIPT_TAG, path)
            self.assertNotRegex(html, JS_HREF, path)
            parsed = _BreakoutParser()
            parsed.feed(html)
            self.assertEqual(parsed.handlers, [], path)
            self.assertEqual(parsed.js_hrefs, [], path)
            self.assertNotIn("<script>alert", html)
            self.assertNotIn("unsafe-inline", hdrs.get("content-security-policy") or "")


if __name__ == "__main__":
    unittest.main()
