"""Presentation / read-model for GET /transparency and the homepage PQ card.

Confirmed evidence comes only from last_confirmed / confirmed_anchors after
independent verify. Current log size is store.size(). Authorized/submitted
are never rendered as confirmed. PQ1 note bytes are reconstructed from
verified fields via encode_note. Does not construct, sign, or broadcast.
Does not scan leaf bodies on GET.
"""

from __future__ import annotations

import base64
import html as html_mod
from datetime import datetime, timezone

from live402.pq import NOTE_FORMAT, NOTE_VERSION, ORIGIN
from live402.pq import algo_anchor
from live402.pq import store
from live402.pq import trust
from live402.pq import worker

_PLACEHOLDER_TXID = frozenset({"", "your_txid", "placeholder", "txid", "none", "null"})
_HARDCODED_LIVE_TXID = "V2HBS4MPRE5SCT62VLVPTGQYANBQAEOMNDYSSVAUTBFRX4PQDE4Q"
_SECRET_MARKERS = (
    "LIVE402_PQ_FALCON_SK",
    "LIVE402_PQ_LOG_SK",
    "LIVE402_PQ_SIGNER_TOKEN",
    "LIVE402_HMAC",
    "HMAC_SECRET",
    "pq-anchor/1",
)
PERA_ADDRESS_URL = "https://testnet.explorer.perawallet.app/address/"


def esc(value) -> str:
    return html_mod.escape("" if value is None else str(value), quote=True)


def utc_text(ts: int) -> str:
    when = int(ts or 0)
    if when <= 0:
        return ""
    return datetime.fromtimestamp(when, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def utc_iso(ts: int) -> str:
    when = int(ts or 0)
    if when <= 0:
        return ""
    return datetime.fromtimestamp(when, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def abbreviate(text: str, head: int = 8, tail: int = 6) -> str:
    raw = str(text or "")
    if len(raw) <= head + tail + 1:
        return raw
    return raw[:head] + "…" + raw[-tail:]


def abbreviate_falcon(address: str) -> str:
    """Algorand base32 account, not hex. Visual: PREFIX…SUFFIX."""
    return abbreviate(address, 8, 7)


def _looks_like_txid(txid: str) -> bool:
    text = str(txid or "").strip()
    low = text.lower()
    if not text or low in _PLACEHOLDER_TXID or "placeholder" in low or text == "YOUR_TXID":
        return False
    try:
        return bool(algo_anchor._looks_like_txid(text))
    except Exception:
        return False


def pera_tx_url(txid: str) -> str:
    if not _looks_like_txid(txid):
        return ""
    try:
        return algo_anchor.testnet_explorer_url(txid.strip())
    except algo_anchor.AnchorError:
        return ""


def pera_address_url(address: str) -> str:
    text = str(address or "").strip()
    if not text or " " in text:
        return ""
    return PERA_ADDRESS_URL + text + "/"


def indexer_tx_url(txid: str) -> str:
    if not _looks_like_txid(txid):
        return ""
    return algo_anchor.TESTNET_INDEXER_TXN_URL + txid.strip()


def root_bytes(root) -> bytes | None:
    if isinstance(root, (bytes, bytearray)):
        raw = bytes(root)
        return raw if len(raw) == 32 else None
    text = str(root or "").strip()
    if not text:
        return None
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return None
    return raw if len(raw) == 32 else None


def root_hex(root) -> str:
    raw = root_bytes(root)
    return raw.hex() if raw else ""


def public_vkey() -> str:
    for source in (store.meta_get("vkey"), trust.vkey()):
        text = str(source or "").strip()
        if text:
            return text
    return ""


def public_falcon_address() -> str:
    return str(algo_anchor.falcon_address() or "").strip()


def decode_pq1_note(note, origin: str | None = None) -> dict | None:
    """Read-only PQ1 decode. Fail closed. Never dumps garbled bytes as body text."""
    if not isinstance(note, (bytes, bytearray)):
        return None
    raw = bytes(note)
    try:
        parsed = algo_anchor.decode_note(raw)
    except (algo_anchor.AnchorError, Exception):
        return None
    if int(parsed.get("version") or 0) != NOTE_VERSION:
        return None
    if str(parsed.get("format") or "") != NOTE_FORMAT:
        return None
    if len(raw) != algo_anchor.NOTE_LEN:
        return None
    want_origin = str(origin or ORIGIN)
    computed = algo_anchor.origin_hash(want_origin)
    note_hash = parsed.get("origin_hash")
    if not isinstance(note_hash, (bytes, bytearray)) or len(note_hash) != 32:
        return None
    return {
        "format": NOTE_FORMAT,
        "version": NOTE_VERSION,
        "origin": want_origin,
        "origin_hash": bytes(note_hash),
        "origin_hash_hex": bytes(note_hash).hex(),
        "computed_origin_hash_hex": computed.hex(),
        "origin_hash_matches": bytes(note_hash) == computed,
        "tree_size": int(parsed.get("tree_size") or 0),
        "root": bytes(parsed.get("root") or b""),
        "root_hex": bytes(parsed.get("root") or b"").hex(),
        "note": raw,
        "note_len": len(raw),
        "note_b64": base64.b64encode(raw).decode("ascii"),
        "note_hex": raw.hex(),
    }


def note_from_confirmed(conf: dict) -> dict | None:
    """Canonical PQ1 from independently verified confirmed fields. Not a chain refetch."""
    raw_root = root_bytes(conf.get("root"))
    if not raw_root:
        return None
    origin = str(conf.get("origin") or ORIGIN)
    size = int(conf.get("size") or 0)
    if size < 1:
        return None
    try:
        note = algo_anchor.encode_note(origin, size, raw_root)
    except algo_anchor.AnchorError:
        return None
    return decode_pq1_note(note, origin)


def confirmed_view() -> dict | None:
    conf = worker.public_anchor()
    if not conf:
        return None
    txid = str(conf.get("txid") or "").strip()
    explorer = str(conf.get("explorer") or "") or pera_tx_url(txid)
    if not explorer:
        return None
    return {
        "size": int(conf.get("size") or 0),
        "at": int(conf.get("at") or 0),
        "txid": txid,
        "round": int(conf.get("round") or 0),
        "root": root_hex(conf.get("root")),
        "origin": str(conf.get("origin") or ORIGIN),
        "explorer": explorer,
        "indexer": indexer_tx_url(txid),
        "utc": utc_text(int(conf.get("at") or 0)),
        "iso": utc_iso(int(conf.get("at") or 0)),
    }


def authorized_lifecycle() -> dict | None:
    auth = worker.last_authorized()
    conf = worker.last_confirmed()
    size = int(auth.get("size") or 0)
    if size < 1:
        return None
    if size <= int(conf.get("size") or 0) and worker.public_anchor():
        return None
    submitted = bool(auth.get("submitted")) and _looks_like_txid(str(auth.get("txid") or ""))
    return {
        "size": size,
        "submitted": submitted,
        "status": "SUBMITTED" if submitted else "AUTHORIZED",
        "label": (
            "SUBMITTED · awaiting TestNet confirmation"
            if submitted
            else "AUTHORIZED · awaiting TestNet confirmation"
        ),
    }


def history_rows() -> list[dict]:
    rows = store.list_confirmed_anchors()
    valid = []
    for row in rows:
        txid = str(row.get("txid") or "").strip()
        size = int(row.get("size") or 0)
        rnd = int(row.get("round") or 0)
        if size < 1 or rnd < 1 or not _looks_like_txid(txid):
            continue
        explorer = pera_tx_url(txid)
        if not explorer:
            continue
        valid.append(
            {
                "size": size,
                "txid": txid,
                "round": rnd,
                "at": int(row.get("at") or 0),
                "utc": utc_text(int(row.get("at") or 0)),
                "iso": utc_iso(int(row.get("at") or 0)),
                "explorer": explorer,
                "root": root_hex(row.get("root")),
            }
        )
    chronological = sorted(valid, key=lambda r: (r["at"], r["size"]))
    prev = 0
    deltas = {}
    spans = {}
    for row in chronological:
        size = row["size"]
        deltas[id(row)] = size if prev <= 0 else size - prev
        if prev <= 0:
            spans[id(row)] = "leaves 1–%s" % size if size >= 1 else ""
        elif size > prev:
            spans[id(row)] = "leaves %s–%s" % (prev + 1, size)
        else:
            spans[id(row)] = ""
        prev = size
    for row in valid:
        row["delta"] = deltas.get(id(row), row["size"])
        row["span"] = spans.get(id(row), "")
    return valid


def page_model() -> dict:
    current = int(store.size() or 0)
    confirmed = confirmed_view()
    confirmed_size = int(confirmed["size"]) if confirmed else 0
    growth = current - confirmed_size if current >= confirmed_size else 0
    history = history_rows()
    note = note_from_confirmed(confirmed) if confirmed else None
    return {
        "current_size": current,
        "confirmed": confirmed,
        "confirmed_size": confirmed_size,
        "growth": growth,
        "caught_up": bool(confirmed) and growth == 0,
        "lifecycle": authorized_lifecycle(),
        "history": history,
        "anchors_confirmed": len(history),
        "note": note,
        "vkey": public_vkey(),
        "falcon_address": public_falcon_address(),
        "checkpoint": str(store.latest_checkpoint() or ""),
    }


def homepage_pq_html() -> str:
    """Homepage PQ card. Empty unless last_confirmed has a real TestNet txid."""
    conf = confirmed_view()
    if not conf:
        return ""
    evidence = (
        '        <p class="pq-evidence">Latest checkpoint · Tree %s · Block %s · Confirmed</p>\n'
        % (esc(conf["size"]), esc(conf["round"]))
    )
    return (
        '      <section class="pq-testnet" id="pq-testnet">\n'
        '        <p class="pq-badge">PQ transparency · TestNet</p>\n'
        "        <h2>Trust the history, too.</h2>\n"
        "        <p>As agents start spending money on your behalf, there should be a "
        "record of what they relied on. 402Signal keeps a tamper-evident history of "
        "its routing evidence and periodically anchors that history to Algorand "
        "TestNet using Falcon-1024 authorization.</p>\n"
        f"{evidence}"
        "        <p>So if an agent pays for an API, research, or service, the evidence "
        "behind that routing decision can’t simply be rewritten later.</p>\n"
        '        <p><a class="btn secondary" href="/transparency">View transparency log</a></p>\n'
        "      </section>\n"
    )


def _copy_btn(value: str, what: str = "value") -> str:
    raw = str(value or "")
    if not raw:
        return ""
    return (
        '<button type="button" class="copy-btn" data-copy="%s" '
        'aria-label="Copy %s">Copy</button>'
        % (esc(raw), esc(what))
    )


def _mono_copy(value: str, display: str | None = None, what: str = "value") -> str:
    raw = str(value or "")
    shown = display if display is not None else raw
    if not raw:
        return ""
    return (
        '<span class="mono-copy"><code class="mono wrap">%s</code> %s</span>'
        % (esc(shown), _copy_btn(raw, what))
    )


def _time(ts_text: str, iso: str) -> str:
    if not ts_text:
        return "-"
    if iso:
        return '<time datetime="%s">%s</time>' % (esc(iso), esc(ts_text))
    return esc(ts_text)


def _ext_link(href: str, label: str) -> str:
    return (
        '<a href="%s" rel="noopener noreferrer">%s '
        '<span class="ext-hint">(Pera Explorer, TestNet)</span></a>'
        % (esc(href), esc(label))
    )


def _status_strip(model: dict) -> str:
    confirmed = model["confirmed"]
    current = model["current_size"]
    confirmed_size = model["confirmed_size"]
    growth = model["growth"]
    n_anchors = model["anchors_confirmed"]
    if confirmed and growth == 0:
        since = "Caught up"
    elif confirmed and growth > 0:
        since = "%s newer log entries since the latest confirmed anchor" % growth
    elif current > 0 and not confirmed:
        since = "Log exists · no confirmed checkpoint yet"
    else:
        since = "No confirmed checkpoint yet"
    latest = _time(confirmed["utc"], confirmed["iso"]) if confirmed else "-"
    last_anchored = str(confirmed_size) if confirmed else "-"
    return (
        '<section class="pq-status" aria-label="Live log status">\n'
        '  <div><p class="pq-kicker">LOG SIZE</p><p class="pq-stat">%s</p></div>\n'
        '  <div><p class="pq-kicker">LAST ANCHORED</p><p class="pq-stat">%s</p></div>\n'
        '  <div><p class="pq-kicker">SINCE ANCHOR</p><p class="pq-stat">%s</p></div>\n'
        '  <div><p class="pq-kicker">ANCHORS CONFIRMED</p><p class="pq-stat">%s</p></div>\n'
        '  <div><p class="pq-kicker">LATEST ANCHOR</p><p class="pq-stat">%s</p></div>\n'
        "</section>\n"
        % (esc(current), esc(last_anchored), esc(since), esc(n_anchors), latest)
    )


def _four_stage() -> str:
    return (
        '<div class="flow flow-four" role="img" '
        'aria-label="Routing evidence, then append-only log, then signed checkpoint, then Algorand TestNet">\n'
        '  <div class="flow-box">\n'
        '    <p class="flow-kicker">ROUTING EVIDENCE</p>\n'
        '    <p class="flow-title">Commitments</p>\n'
        "    <p class=\"flow-detail\">Commitments to what 402Signal observed and selected.</p>\n"
        "  </div>\n"
        '  <div class="flow-conn" aria-hidden="true"></div>\n'
        '  <div class="flow-box">\n'
        '    <p class="flow-kicker">APPEND-ONLY LOG</p>\n'
        '    <p class="flow-title">Merkle history</p>\n'
        "    <p class=\"flow-detail\">A growing Merkle history that exposes later rewriting.</p>\n"
        "  </div>\n"
        '  <div class="flow-conn" aria-hidden="true"></div>\n'
        '  <div class="flow-box">\n'
        '    <p class="flow-kicker">SIGNED CHECKPOINT</p>\n'
        '    <p class="flow-title">Ed25519 identity</p>\n'
        "    <p class=\"flow-detail\">402Signal signs the current Merkle root with its Ed25519 log identity.</p>\n"
        "  </div>\n"
        '  <div class="flow-conn" aria-hidden="true"></div>\n'
        '  <div class="flow-box flow-emphasis">\n'
        '    <p class="flow-kicker">ALGORAND TESTNET</p>\n'
        '    <p class="flow-title">Falcon-1024 / f1</p>\n'
        "    <p class=\"flow-detail\">The checkpoint is anchored using Falcon-1024 / f1 authorization.</p>\n"
        "  </div>\n"
        "</div>\n"
        "<p class=\"note\">Proof chain: routing evidence is committed to an append-only log; "
        "402Signal signs a checkpoint of that log; the checkpoint is authorized on Algorand "
        "TestNet with Falcon-1024 (f1).</p>\n"
    )


def _pera_views(model: dict) -> str:
    conf = model["confirmed"]
    addr = model["falcon_address"]
    items = []
    if conf and conf.get("explorer"):
        items.append(
            "<li>%s</li>"
            % _ext_link(conf["explorer"], "View latest anchor on Pera")
        )
    account = pera_address_url(addr)
    if account:
        items.append(
            "<li>%s. This is the configured Falcon-1024 account. "
            "Not every transaction on that account is a valid 402Signal checkpoint "
            "without PQ1 verification. First-party confirmed history is listed below.</li>"
            % _ext_link(account, "View all anchors on Pera")
        )
    if not items:
        return ""
    return (
        '<section class="block" id="pera-views">\n'
        "  <h2>TestNet explorers</h2>\n"
        '  <ul class="verify-list">%s</ul>\n'
        "</section>\n" % "".join(items)
    )


def _confirmed_card(model: dict) -> str:
    conf = model["confirmed"]
    if not conf:
        return (
            '<section class="block" id="latest-confirmed">\n'
            "  <h2>Latest confirmed checkpoint</h2>\n"
            "  <p>TestNet anchoring has not yet produced a confirmed checkpoint.</p>\n"
            "</section>\n"
        )
    note = model["note"]
    decoder = _pq1_decoder(note) if note else (
        "<p>The committed note could not be reconstructed from verified fields.</p>\n"
    )
    return (
        '<section class="block" id="latest-confirmed">\n'
        "  <h2>Latest confirmed checkpoint</h2>\n"
        '  <article class="panel confirm-card">\n'
        '    <p class="pq-kicker">Status</p>\n'
        "    <p>Confirmed</p>\n"
        '    <p class="pq-kicker">Tree size</p>\n'
        "    <p>%s</p>\n"
        '    <p class="pq-kicker">Merkle root</p>\n'
        "    <p>%s</p>\n"
        '    <p class="pq-kicker">Origin</p>\n'
        "    <p>402signal.com/pq/log</p>\n"
        '    <p class="pq-kicker">Confirmed</p>\n'
        "    <p>%s</p>\n"
        '    <p class="pq-kicker">Block / round</p>\n'
        "    <p>%s</p>\n"
        '    <p class="pq-kicker">Transaction</p>\n'
        "    <p>%s</p>\n"
        '    <p class="pq-kicker">Authorization</p>\n'
        "    <p>Falcon-1024 (f1)</p>\n"
        "%s"
        '    <p class="pq-kicker">Amount</p>\n'
        "    <p>0 ALGO</p>\n"
        '    <p class="hero-actions">\n'
        '      <a class="btn" href="%s" rel="noopener noreferrer">View latest anchor on Pera</a>\n'
        '      <a class="btn secondary" href="/pq/log/checkpoint">View signed checkpoint</a>\n'
        "    </p>\n"
        "  </article>\n"
        '  <div class="pq-decoder">\n'
        "    <h3>What the transaction commits</h3>\n"
        "    <p>Generic explorers may show unreadable text. 402Signal decodes the same 84-byte PQ1 layout.</p>\n"
        "%s"
        "  </div>\n"
        "</section>\n"
        % (
            esc(conf["size"]),
            _mono_copy(conf["root"], what="Merkle root"),
            _time(conf["utc"], conf["iso"]),
            esc(conf["round"]),
            _mono_copy(conf["txid"], abbreviate(conf["txid"]), "transaction id"),
            _falcon_account_row(model["falcon_address"]),
            esc(conf["explorer"]),
            decoder,
        )
    )


def _falcon_account_row(address: str) -> str:
    text = str(address or "").strip()
    if not text:
        return ""
    return (
        '    <p class="pq-kicker">Falcon account</p>\n'
        "    <p>%s</p>\n"
        % _mono_copy(text, abbreviate_falcon(text), "Falcon account")
    )


def _pq1_decoder(note: dict) -> str:
    match = "matches note origin-hash bytes" if note["origin_hash_matches"] else "does not match"
    return (
        "<p>Canonical PQ1 note. Reconstructed from the fields independently verified "
        "against the confirmed TestNet transaction.</p>\n"
        '<dl class="pq-decode">\n'
        "  <div><dt>FORMAT</dt><dd>%s</dd></div>\n"
        "  <div><dt>VERSION</dt><dd>%s</dd></div>\n"
        "  <div><dt>EXPECTED ORIGIN</dt><dd>%s</dd></div>\n"
        "  <div><dt>ORIGIN HASH</dt><dd>SHA-256 of origin UTF-8: %s · %s</dd></div>\n"
        "  <div><dt>TREE SIZE</dt><dd>%s</dd></div>\n"
        "  <div><dt>MERKLE ROOT</dt><dd>%s</dd></div>\n"
        "  <div><dt>NOTE LENGTH</dt><dd>%s bytes</dd></div>\n"
        "</dl>\n"
        % (
            esc(note["format"]),
            esc(note["version"]),
            esc(note["origin"]),
            _mono_copy(note["computed_origin_hash_hex"], what="origin hash"),
            esc(match),
            esc(note["tree_size"]),
            _mono_copy(note["root_hex"], what="Merkle root"),
            esc(note["note_len"]),
        )
    )


def _current_vs_anchored(model: dict) -> str:
    current = model["current_size"]
    confirmed_size = model["confirmed_size"]
    growth = model["growth"]
    if model["confirmed"] and growth == 0:
        compare = "The latest log checkpoint is anchored."
    elif model["confirmed"] and growth > 0:
        compare = "%s newer log entries exist after the latest confirmed anchor." % growth
    elif current > 0 and not model["confirmed"]:
        compare = "The log has entries, and TestNet anchoring has not yet produced a confirmed checkpoint."
    else:
        compare = "TestNet anchoring has not yet produced a confirmed checkpoint."
    lifecycle = ""
    if model["lifecycle"]:
        lifecycle = "<p>%s</p>\n" % esc(model["lifecycle"]["label"])
    return (
        '<section class="block" id="current-vs-anchored">\n'
        "  <h2>Current vs anchored</h2>\n"
        "  <p>Current tree %s · confirmed tree %s · unanchored growth %s.</p>\n"
        "  <p>%s</p>\n"
        "%s"
        "</section>\n"
        % (
            esc(current),
            esc(confirmed_size if model["confirmed"] else 0),
            esc(growth),
            esc(compare),
            lifecycle,
        )
    )


def _history(model: dict) -> str:
    rows = model["history"]
    n = model["anchors_confirmed"]
    heading = "<p>TOTAL CONFIRMED ANCHORS %s</p>\n" % esc(n)
    if not rows:
        return (
            '<section class="block" id="anchor-history">\n'
            "  <h2>Anchor history</h2>\n"
            + heading
            + "</section>\n"
        )
    cards = []
    body = []
    for row in rows:
        span = (" · %s" % row["span"]) if row.get("span") else ""
        when = _time(row["utc"], row["iso"])
        link = _ext_link(row["explorer"], abbreviate(row["txid"]))
        body.append(
            "<tr>"
            "<td>%s%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            "</tr>"
            % (esc(row["size"]), esc(span), esc(row["delta"]), when, esc(row["round"]), link)
        )
        cards.append(
            '<article class="history-card">'
            "<p>TREE %s%s</p><p>Δ LEAVES %s</p><p>CONFIRMED %s</p>"
            "<p>BLOCK %s</p><p>TRANSACTION %s</p></article>"
            % (esc(row["size"]), esc(span), esc(row["delta"]), when, esc(row["round"]), link)
        )
    return (
        '<section class="block" id="anchor-history">\n'
        "  <h2>Anchor history</h2>\n"
        + heading
        + '  <div class="table-wrap history-table">\n'
        '    <table class="anchor-table">\n'
        "      <thead><tr><th scope=\"col\">TREE</th><th scope=\"col\">Δ LEAVES</th>"
        "<th scope=\"col\">CONFIRMED</th><th scope=\"col\">BLOCK</th>"
        "<th scope=\"col\">TRANSACTION</th></tr></thead>\n"
        "      <tbody>%s</tbody>\n"
        "    </table>\n"
        "  </div>\n"
        '  <div class="history-cards">%s</div>\n'
        "%s"
        "</section>\n"
        % ("".join(body), "".join(cards), _growth_chart(rows))
    )


def _growth_chart(rows: list[dict]) -> str:
    if len(rows) < 2:
        return ""
    chronological = sorted(rows, key=lambda r: (r["at"], r["size"]))
    times = [int(r["at"]) for r in chronological]
    sizes = [int(r["size"]) for r in chronological]
    min_t, max_t = min(times), max(times)
    min_s, max_s = min(sizes), max(sizes)
    span_t = max(max_t - min_t, 1)
    span_s = max(max_s - min_s, 1)
    width, height = 320, 120
    pad_l, pad_r, pad_t, pad_b = 28, 8, 8, 22
    inner_w = width - pad_l - pad_r
    inner_h = height - pad_t - pad_b
    points = []
    circles = []
    for ts, size in zip(times, sizes):
        x = pad_l + inner_w * ((ts - min_t) / span_t)
        y = pad_t + inner_h * (1 - ((size - min_s) / span_s))
        points.append("%.1f,%.1f" % (x, y))
        circles.append('<circle cx="%.1f" cy="%.1f" r="3.5" fill="#fec865" />' % (x, y))
    return (
        '<figure class="growth-chart">\n'
        "  <figcaption>Confirmed tree size at each confirmed time. "
        "The line joins those observations only.</figcaption>\n"
        '  <svg viewBox="0 0 %s %s" role="img" '
        'aria-label="Confirmed tree size at each confirmed timestamp. '
        'Points are real confirmed checkpoints. The line joins those observations.">\n'
        '    <polyline fill="none" stroke="#e49c60" stroke-width="1.5" points="%s" />\n'
        "    %s\n"
        "  </svg>\n"
        "</figure>\n"
        % (width, height, " ".join(points), "".join(circles))
    )


def _technical(model: dict) -> str:
    conf = model["confirmed"]
    note = model["note"]
    parts = []
    parts.append("<p>Authorization · Falcon-1024 · f1</p>")
    if note:
        parts.append(
            "<p>Canonical PQ1 note · reconstructed from independently verified "
            "origin, tree size, and Merkle root.</p>"
        )
        parts.append(
            '<p>Canonical PQ1 note · reconstructed (base64)</p>'
            '<div class="scroll-block"><p class="mono">%s</p></div>'
            % esc(note["note_b64"])
        )
        parts.append(
            '<p>Canonical PQ1 note · reconstructed (hex)</p>'
            '<div class="scroll-block"><p class="mono">%s</p></div>'
            % esc(note["note_hex"])
        )
        parts.append("<p>Note length · %s bytes</p>" % esc(note["note_len"]))
        parts.append("<p>Origin hash · %s</p>" % esc(note["origin_hash_hex"]))
    if model["checkpoint"]:
        parts.append(
            "<p>Signed Ed25519 checkpoint</p>"
            '<div class="scroll-block"><pre class="code">%s</pre></div>'
            % esc(model["checkpoint"])
        )
    if model["vkey"]:
        parts.append(
            "<p>Public Ed25519 vkey</p><p class=\"mono wrap\">%s %s</p>"
            % (esc(model["vkey"]), _copy_btn(model["vkey"], "public log vkey"))
        )
    if model["falcon_address"]:
        parts.append(
            "<p>Public Falcon address (Algorand base32)</p><p>%s</p>"
            % _mono_copy(
                model["falcon_address"],
                abbreviate_falcon(model["falcon_address"]),
                "Falcon account",
            )
        )
    if conf:
        parts.append(
            "<p>Txid · %s</p>"
            % _mono_copy(conf["txid"], abbreviate(conf["txid"]), "transaction id")
        )
        parts.append("<p>Confirmed round · %s</p>" % esc(conf["round"]))
        if conf.get("indexer"):
            parts.append(
                '<p><a href="%s" rel="noopener noreferrer">View raw TestNet transaction JSON</a> '
                '<span class="ext-hint">(Algonode TestNet indexer)</span></p>'
                % esc(conf["indexer"])
            )
    body = "\n".join(parts) if parts else "<p>No additional public fields yet.</p>"
    return (
        '<details class="tech-details">\n'
        "  <summary>Technical details</summary>\n"
        '  <div class="tech-body">%s</div>\n'
        "</details>\n" % body
    )


def _verify_yourself(model: dict) -> str:
    conf = model["confirmed"]
    copies = [("Origin", ORIGIN, ORIGIN, "origin")]
    copies.append(
        (
            "Tree size",
            str(model["confirmed_size"] if conf else model["current_size"]),
            None,
            "tree size",
        )
    )
    if conf and conf.get("root"):
        copies.append(("Merkle root", conf["root"], None, "Merkle root"))
    if conf and conf.get("txid"):
        copies.append(("Txid", conf["txid"], abbreviate(conf["txid"]), "transaction id"))
    if model["vkey"]:
        copies.append(("Public log vkey", model["vkey"], None, "public log vkey"))
    rows = []
    for label, value, display, what in copies:
        rows.append(
            "<li><span>%s</span> %s</li>"
            % (esc(label), _mono_copy(value, display, what))
        )
    return (
        '<section class="block" id="verify-yourself">\n'
        "  <h2>Verify yourself</h2>\n"
        "  <ul class=\"verify-list\">\n"
        '    <li><a href="/pq/log/checkpoint">GET /pq/log/checkpoint</a> (signed Ed25519 checkpoint)</li>\n'
        "    <li>C2SP tiles are published under <code>/pq/log/tile/</code> "
        "(hash tiles and entry bundles). Compare them to the signed checkpoint.</li>\n"
        "  </ul>\n"
        "  <ul class=\"verify-copies\">%s</ul>\n"
        "</section>\n" % "".join(rows)
    )


def _chrome_head(title: str, description: str, canonical: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8" />\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        "  <title>%s</title>\n"
        '  <meta name="description" content="%s" />\n'
        '  <link rel="canonical" href="%s" />\n'
        '  <meta property="og:title" content="%s" />\n'
        '  <meta property="og:description" content="%s" />\n'
        '  <meta property="og:url" content="%s" />\n'
        '  <meta property="og:type" content="website" />\n'
        '  <meta property="og:site_name" content="402Signal" />\n'
        '  <meta name="twitter:card" content="summary" />\n'
        '  <meta name="twitter:title" content="%s" />\n'
        '  <meta name="twitter:description" content="%s" />\n'
        '  <meta name="twitter:site" content="@402Signal" />\n'
        '  <link rel="stylesheet" href="/styles.css" />\n'
        '  <script src="/transparency.js" defer></script>\n'
        "</head>\n"
        % (
            esc(title),
            esc(description),
            esc(canonical),
            esc(title),
            esc(description),
            esc(canonical),
            esc(title),
            esc(description),
        )
    )


def _site_header() -> str:
    return (
        '    <header class="site">\n'
        '      <a class="brand" href="/">\n'
        '        <span class="mark">402</span>\n'
        '        <span class="brand-name">402Signal</span>\n'
        "      </a>\n"
        '      <nav class="nav" aria-label="Primary">\n'
        '        <a href="/catalog">Catalog</a>\n'
        '        <a href="/how">How it works</a>\n'
        '        <a href="/developers">Developers</a>\n'
        '        <a href="https://github.com/402signal/402signal" rel="noopener noreferrer">GitHub</a>\n'
        "      </nav>\n"
        "    </header>\n"
    )


def _site_footer(*, current: str = "") -> str:
    trans = (
        '        <a href="/transparency" aria-current="page">Transparency</a>\n'
        if current == "/transparency"
        else '        <a href="/transparency">Transparency</a>\n'
    )
    return (
        '    <footer class="foot">\n'
        '      <p class="listed-on">\n'
        "        Listed on\n"
        '        <a href="https://glama.ai/mcp/servers/402signal/402signal" rel="noopener noreferrer">Glama</a>\n'
        '        <a href="https://registry.modelcontextprotocol.io/?q=402signal" rel="noopener noreferrer">MCP Registry</a>\n'
        '        <a href="https://github.com/Haustorium12/gold-402/blob/main/directory/aggregators.md" rel="noopener noreferrer">Gold-402</a>\n'
        '        <a href="https://smithery.ai/servers/live402/signal" rel="noopener noreferrer">Smithery</a>\n'
        '        <a href="https://agentic.market/services/402signal-com" rel="noopener noreferrer">Agentic Market</a>\n'
        '        <a href="https://github.com/michielpost/x402-dev/blob/master/Projects.md" rel="noopener noreferrer">x402-dev</a>\n'
        '        <a href="https://facilitator.goplausible.xyz/dashboard/bazaar?q=402signal" rel="noopener noreferrer">GoPlausible</a>\n'
        "      </p>\n"
        "      <p>\n"
        '        <a href="https://github.com/402signal/402signal" rel="noopener noreferrer">GitHub</a>\n'
        '        <a href="https://x.com/402Signal" rel="noopener noreferrer">@402Signal</a>\n'
        '        <a href="/openapi.json">OpenAPI</a>\n'
        + trans
        + '        <a href="mailto:402signal@gmail.com">Contact</a>\n'
        "      </p>\n"
        "    </footer>\n"
    )


def render_html() -> str:
    return page_html()


def page_html() -> str:
    model = page_model()
    title = "402Signal Transparency — Verify the routing history"
    description = (
        "Verify 402Signal’s append-only routing-evidence history, signed checkpoints "
        "and Falcon-authorized Algorand TestNet anchors."
    )
    html = (
        _chrome_head(title, description, "https://402signal.com/transparency")
        + "<body>\n"
        '  <div class="page">\n'
        + _site_header()
        + "    <main>\n"
        + _main(model)
        + "    </main>\n"
        + _site_footer(current="/transparency")
        + '    <p class="copy-status" id="copy-status" role="status" aria-live="polite"></p>\n'
        "  </div>\n"
        "</body>\n"
        "</html>\n"
    )
    assert_no_secrets(html)
    return html


def _main(model: dict) -> str:
    return (
        '      <section class="hero compact">\n'
        '        <p class="pq-badge">PQ transparency · Algorand TestNet</p>\n'
        "        <h1>Verify 402Signal’s history.</h1>\n"
        "        <p class=\"lede\">402Signal records commitments to its routing evidence in an "
        "append-only transparency log. Signed checkpoints are periodically anchored to "
        "Algorand TestNet using native Falcon-1024 authorization.</p>\n"
        "        <p class=\"note\">Routing never waits for blockchain confirmation.</p>\n"
        "      </section>\n"
        '      <section class="block">\n'
        "        <h2>Why this matters</h2>\n"
        "        <p>Imagine your shopping agent spends $3 checking whether a used car is a good buy. "
        "Months later, you want to know which services it relied on.</p>\n"
        "        <p>402Signal’s transparency log creates a timestamped, tamper-evident record of the "
        "routing evidence it produced. The blockchain anchor makes it much harder for that "
        "historical record to be silently rewritten after the fact.</p>\n"
        "        <p>It doesn’t reveal your wallet or raw request, and it doesn’t prove an endpoint "
        "was truthful. It gives you a way to verify the history of the decision infrastructure "
        "your agent relied on.</p>\n"
        "        <p>The same idea applies when an agent compares paid news, buys a report, selects "
        "an API, purchases data, or pays for compute.</p>\n"
        "        <p class=\"note\">What did my agent rely on when it spent my money?</p>\n"
        "      </section>\n"
        + _status_strip(model)
        + _four_stage()
        + _confirmed_card(model)
        + _pera_views(model)
        + _current_vs_anchored(model)
        + _history(model)
        + _technical(model)
        + '      <section class="block">\n'
        "        <h2>What this proves</h2>\n"
        "        <p>This anchor commits a specific log state to TestNet. Anyone can compare the "
        "signed checkpoint, Merkle root, and confirmed txn to detect later rewriting.</p>\n"
        "        <h2>What this does not prove</h2>\n"
        "        <p>It does not prove an endpoint claim was true. It is not a merchant payment.</p>\n"
        "        <p>402Signal recommends the route. The agent retains the wallet, signing, and "
        "seller payment.</p>\n"
        "        <p>It does not: let the blockchain prove endpoint correctness; make Base or Solana "
        "payments PQ-safe via Falcon; identify or authenticate a seller; or guarantee future "
        "endpoint behavior.</p>\n"
        "      </section>\n"
        + _verify_yourself(model)
    )


def assert_no_secrets(html: str) -> None:
    low = html.lower()
    for marker in _SECRET_MARKERS:
        if marker.lower() in low:
            raise ValueError("secret marker in html")
    if _HARDCODED_LIVE_TXID in html:
        raise ValueError("hardcoded live txid in html")
