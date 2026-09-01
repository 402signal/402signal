"""Shared human-site chrome. Presentation only. No routing or PQ construction."""

from __future__ import annotations

import html as html_mod

CONTACT_EMAIL = "ross@402signal.com"
CONTACT_MAILTO = "mailto:ross@402signal.com"

NAV = (
    ("/", "Home"),
    ("/how", "How it works"),
    ("/catalog", "Explore"),
    ("/developers", "Developers"),
    ("/contact", "Contact"),
)

FOOTER = (
    ("https://github.com/402signalhq/402signal", "GitHub", True),
    ("https://x.com/402Signal", "@402Signal", True),
    ("/openapi.json", "OpenAPI", False),
    ("/mcp.json", "MCP", False),
    ("/transparency", "Transparency", False),
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
        links.append('        <a href="%s"%s>%s</a>' % (esc(href), cur, esc(label)))
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
        "        <h2>Public directories</h2>\n"
        "        <p>402Signal is currently listed in the following public directories "
        "and registries.</p>\n"
        '        <p class="listed-on">\n'
        + listed_on_items_html()
        + "\n        </p>\n"
        "        <p class=\"note\">These links confirm directory presence; they are not "
        "endorsements.</p>\n"
        "      </section>\n"
    )


def listed_on_html(*, title: str = "Listed on", note: str = "") -> str:
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
    """One homepage routing diagram. variant is ignored; proof graphics are not used."""
    del variant
    return (
        '<figure class="signal-flow" aria-labelledby="signal-flow-caption">\n'
        '  <div class="signal-row flow-four">\n'
        '    <div class="flow-box">\n'
        '      <p class="flow-kicker">DISCOVERY</p>\n'
        '      <p class="flow-title">Search supported discovery sources</p>\n'
        "    </div>\n"
        '    <div class="flow-conn gold" aria-hidden="true"></div>\n'
        '    <div class="flow-box flow-emphasis">\n'
        '      <p class="flow-kicker">LIVE CHECK</p>\n'
        '      <p class="flow-title">Probe candidate endpoints and parse current HTTP 402 payment requirements</p>\n'
        "    </div>\n"
        '    <div class="flow-conn gold" aria-hidden="true"></div>\n'
        '    <div class="flow-box">\n'
        '      <p class="flow-kicker">POLICY</p>\n'
        '      <p class="flow-title">Apply caller constraints and rank eligible observed candidates</p>\n'
        "    </div>\n"
        '    <div class="flow-conn gold" aria-hidden="true"></div>\n'
        '    <div class="flow-box">\n'
        '      <p class="flow-kicker">RESULT</p>\n'
        '      <p class="flow-title">Return selected route + selected_payment, or a typed miss</p>\n'
        "    </div>\n"
        "  </div>\n"
        '  <div class="trust-rail">\n'
        '    <p class="trust-rail-label">TRANSPARENCY</p>\n'
        '    <ol class="trust-steps">\n'
        '      <li>Commit route evidence to the append-only log. Production log identity '
        "is Algorand MainNet. Awaiting first confirmed MainNet checkpoint.</li>\n"
        "    </ol>\n"
        "  </div>\n"
        '  <figcaption class="sr-only" id="signal-flow-caption">Discovery, live check, '
        "policy, and result, with an append-only transparency log</figcaption>\n"
        "</figure>\n"
    )
