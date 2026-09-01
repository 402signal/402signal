"""Shared human-site chrome. Presentation only. No routing or PQ construction."""

from __future__ import annotations

import html as html_mod

NAV = (
    ("/catalog", "Catalog"),
    ("/how", "How it works"),
    ("/developers", "Developers"),
)

FOOTER = (
    ("https://github.com/402signal/402signal", "GitHub", True),
    ("https://x.com/402Signal", "@402Signal", True),
    ("/openapi.json", "OpenAPI", False),
    ("/transparency", "Transparency", False),
    ("mailto:402signal@gmail.com", "Contact", False),
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
        links.append('        <a href="%s"%s%s>%s</a>' % (esc(href), rel, cur, esc(label)))
    return (
        '    <footer class="foot">\n'
        "      <p>\n"
        + "\n".join(links)
        + "\n      </p>\n"
        "    </footer>\n"
    )


def listed_on_html() -> str:
    items = []
    for href, label in LISTED_ON:
        items.append(
            '        <a href="%s" rel="noopener noreferrer">%s</a>'
            % (esc(href), esc(label))
        )
    return (
        '      <details class="ecosystem">\n'
        "        <summary>Listed on</summary>\n"
        '        <p class="listed-on">\n'
        + "\n".join(items)
        + "\n        </p>\n"
        "      </details>\n"
    )


def signal_flow_html(*, variant: str = "product") -> str:
    """Discovery → 402Signal → Your Agent, plus a teal evidence rail.

    variant:
      product  home / how
      proof    transparency (more technical detail on the rail)
    """
    if variant == "proof":
        discovery_title = "Catalog claims"
        discovery_detail = "What discovery sources listed. A claim is not an observation."
        check_title = "Independent check"
        check_detail = "Observed 402 envelope, payment terms, invocation info, freshness."
        agent_title = "Agent decision"
        agent_detail = "Route or miss. Wallet, seller payment, and the call stay with the agent."
        rail_items = (
            (
                "COMMIT",
                "Observed routing evidence is appended to a Merkle log "
                "(C2SP tiles, origin 402signal.com/pq/log).",
            ),
            (
                "SIGN",
                "402Signal signs a tlog-checkpoint of that tree with its Ed25519 log identity.",
            ),
            (
                "ANCHOR",
                "Eligible signed checkpoints may be authorized on Algorand TestNet "
                "with Falcon-1024 (f1). 0 ALGO. Routing never waits.",
            ),
        )
        aria = (
            "Discovery to 402Signal to your agent. Teal evidence path: "
            "commit, sign, then Algorand TestNet Falcon-1024 anchor"
        )
        note = (
            "Gold is the immediate route (synchronous). Teal is evidence "
            "(asynchronous). Algorand is not a fourth commerce stage."
        )
    else:
        discovery_title = "What catalogs claim"
        discovery_detail = "Listings and seller metadata. A claim can outlive the endpoint."
        check_title = "Check it now"
        check_detail = "Valid x402? Payment terms? Invocation info? Fresh observation?"
        agent_title = "Decide whether to spend"
        agent_detail = "Own wallet. Sign. Pay the seller. Call the API."
        rail_items = (
            ("COMMIT", "Routing evidence enters an append-only Merkle log."),
            ("SIGN", "402Signal signs a checkpoint with its Ed25519 log identity."),
            ("ANCHOR", "Algorand TestNet · Falcon-1024"),
        )
        aria = (
            "Discovery to 402Signal to your agent, with an asynchronous teal evidence path"
        )
        note = (
            "The route is gold and synchronous. Evidence is teal and does not delay routing."
        )
    steps = []
    for kicker, text in rail_items:
        steps.append(
            "        <li><span class=\"trust-step-kicker\">%s</span> %s</li>"
            % (esc(kicker), esc(text))
        )
    head = (
        '<div class="signal-flow" role="img" aria-label="%s">\n'
        '  <div class="signal-row">\n'
        '    <div class="flow-box">\n'
        '      <p class="flow-kicker">DISCOVERY</p>\n'
        '      <p class="flow-title">%s</p>\n'
        '      <p class="flow-detail">%s</p>\n'
        "    </div>\n"
        '    <div class="flow-conn gold" aria-hidden="true"></div>\n'
        '    <div class="flow-box flow-emphasis">\n'
        '      <p class="flow-kicker">402SIGNAL</p>\n'
        '      <p class="flow-title">%s</p>\n'
        '      <p class="flow-detail">%s</p>\n'
        "    </div>\n"
        '    <div class="flow-conn gold" aria-hidden="true"></div>\n'
        '    <div class="flow-box">\n'
        '      <p class="flow-kicker">YOUR AGENT</p>\n'
        '      <p class="flow-title">%s</p>\n'
        '      <p class="flow-detail">%s</p>\n'
        "    </div>\n"
        "  </div>\n"
        '  <div class="trust-rail">\n'
        '    <p class="trust-rail-label">EVIDENCE · asynchronous</p>\n'
        '    <ol class="trust-steps">\n'
        % (
            esc(aria),
            esc(discovery_title),
            esc(discovery_detail),
            esc(check_title),
            esc(check_detail),
            esc(agent_title),
            esc(agent_detail),
        )
    )
    return (
        head
        + "\n".join(steps)
        + "\n    </ol>\n"
        + ('    <p class="trust-rail-note">%s</p>\n' % esc(note))
        + "  </div>\n"
        + "</div>\n"
    )
