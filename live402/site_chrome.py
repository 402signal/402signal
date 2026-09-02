"""Shared human-site chrome. Presentation only. No routing or PQ construction."""

from __future__ import annotations

import html as html_mod

CONTACT_EMAIL = "ross@402signal.com"
CONTACT_MAILTO = "mailto:ross@402signal.com"

NAV = (
    ("/catalog", "Explore"),
    ("/developers", "Developers"),
    ("/transparency", "Transparency"),
    ("https://github.com/402signalhq/402signal", "GitHub"),
)

FOOTER = (
    ("https://github.com/402signalhq/402signal", "GitHub", True),
    ("https://x.com/402Signal", "@402Signal", True),
    ("/how", "How it works", False),
    ("/openapi.json", "OpenAPI", False),
    ("/mcp.json", "MCP", False),
    ("/transparency", "Transparency", False),
    ("/contact", "Contact", False),
    (CONTACT_MAILTO, CONTACT_EMAIL, False),
)

LISTED_ON = (
    ("https://glama.ai/mcp/servers/402signal/402signal", "Glama"),
    ("https://registry.modelcontextprotocol.io/?q=402signal", "MCP Registry"),
    ("https://github.com/Haustorium12/gold-402/blob/main/directory/aggregators.md", "Gold-402"),
    ("https://smithery.ai/servers/live402/signal", "Smithery"),
    ("https://agentic.market/services/402signal-com", "Agentic Market"),
    ("https://github.com/michielpost/x402-dev/blob/master/Projects.md", "x402-dev"),
    ("https://facilitator.goplausible.xyz/dashboard/bazaar?q=402signal", "GoPlausible"),
)


def esc(value) -> str:
    return html_mod.escape("" if value is None else str(value), quote=True)


def header_html(current: str = "") -> str:
    links = []
    for href, label in NAV:
        cur = ' aria-current="page"' if current == href else ""
        rel = ' rel="noopener noreferrer"' if href.startswith("https://") else ""
        links.append(
            '        <a href="%s"%s%s>%s</a>'
            % (esc(href), rel, cur, esc(label))
        )
    return (
        '    <header class="site">\n'
        '      <a class="brand" href="/">\n'
        '        <span class="mark">402</span>\n'
        '        <span class="brand-name">402Signal</span>\n'
        "      </a>\n"
        '      <nav class="nav" aria-label="Primary">\n'
        + "\n".join(links)
        + "\n      </nav>\n"
        "    </header>\n"
    )


def footer_html(current: str = "") -> str:
    links = []
    for href, label, external in FOOTER:
        rel = ' rel="noopener noreferrer"' if external else ""
        cur = ' aria-current="page"' if current == href else ""
        links.append(
            '        <a href="%s"%s%s>%s</a>' % (esc(href), rel, cur, esc(label))
        )
    return (
        '    <footer class="foot">\n'
        "      <p>402Signal</p>\n"
        "      <p>\n"
        + "\n".join(links)
        + "\n      </p>\n"
        "    </footer>\n"
    )


def listed_on_items_html() -> str:
    items = []
    for href, label in LISTED_ON:
        items.append(
            '        <a href="%s" rel="noopener noreferrer">%s</a>'
            % (esc(href), esc(label))
        )
    return "\n".join(items)


def listed_on_row_html() -> str:
    return (
        '      <section class="discover-row" aria-label="Public directories">\n'
        "        <h2>Listed in</h2>\n"
        "        <p>Directory links show where 402Signal is listed. They are not "
        "endorsements.</p>\n"
        '        <p class="listed-on">\n'
        + listed_on_items_html()
        + "\n        </p>\n"
        "      </section>\n"
    )


def listed_on_html(*, title: str = "Listed in", note: str = "") -> str:
    extra = ""
    if note:
        extra = '        <p class="note">%s</p>\n' % esc(note)
    return (
        '      <details class="ecosystem">\n'
        "        <summary>%s</summary>\n"
        '        <p class="listed-on">\n'
        % esc(title)
        + listed_on_items_html()
        + "\n        </p>\n"
        + extra
        + "      </details>\n"
    )


def signal_flow_html(*, variant: str = "product") -> str:
    """Homepage how-it-works cards. variant is ignored."""
    del variant
    return (
        '<section class="block" id="how-it-works">\n'
        "  <h2>How it works</h2>\n"
        '  <div class="decision-grid">\n'
        '    <article class="decision-card">\n'
        "      <h3>Search</h3>\n"
        "      <p>Find candidates across supported x402 discovery sources.</p>\n"
        "    </article>\n"
        '    <article class="decision-card">\n'
        "      <h3>Check</h3>\n"
        "      <p>Call candidate endpoints and read the payment requirements they "
        "return now.</p>\n"
        "    </article>\n"
        '    <article class="decision-card">\n'
        "      <h3>Match</h3>\n"
        "      <p>Apply your network, price, latency, and invocation constraints.</p>\n"
        "    </article>\n"
        '    <article class="decision-card">\n'
        "      <h3>Return</h3>\n"
        "      <p>Get the best qualifying route, or a typed reason nothing "
        "matched.</p>\n"
        "    </article>\n"
        "  </div>\n"
        "</section>\n"
    )
