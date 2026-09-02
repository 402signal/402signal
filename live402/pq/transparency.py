"""Presentation / read-model for GET /transparency and the homepage PQ card.

Confirmed evidence comes only from last_confirmed / confirmed_anchors after
independent verify. Current log size is store.size(). Authorized/submitted
are never rendered as confirmed. Explorer links are chosen from independently
confirmed anchor network, never from env or the presence of MainNet secrets.
Unverified MainNet explorer URLs are suppressed. PQ1 note bytes are
reconstructed from verified fields via encode_note. Does not construct,
sign, or broadcast. Does not scan leaf bodies on GET.
"""

from __future__ import annotations

import base64
import html as html_mod
from datetime import datetime, timezone

from live402.pq import NOTE_FORMAT, NOTE_VERSION, ORIGIN
from live402.pq import algo_anchor
from live402.pq import checkpoint as ckpt
from live402.pq import store
from live402.pq import trust
from live402.pq import worker
from live402 import site_chrome

_PLACEHOLDER_TXID = frozenset({"", "your_txid", "placeholder", "txid", "none", "null"})
_SECRET_MARKERS = (
    "LIVE402_PQ_FALCON_SK",
    "LIVE402_PQ_LOG_SK",
    "LIVE402_PQ_LOG_SK_MAINNET",
    "LIVE402_PQ_SIGNER_TOKEN",
    "LIVE402_PQ_SIGNER_MAINNET_TOKEN",
    "LIVE402_HMAC",
    "HMAC_SECRET",
)
HISTORY_LIMIT = 250
PERA_ADDRESS_URL = "https://testnet.explorer.perawallet.app/address/"
PERA_ADDRESS_URL_MAINNET = "https://explorer.perawallet.app/address/"


def _live_network_name() -> str:
    from live402.pq import log_identity

    try:
        return log_identity.live_network_name()
    except log_identity.ConfigError:
        return ""


def _network_label(network: str | None = None) -> str:
    """Live identity label. Unknown never becomes TestNet. Confirmed explorers use _confirmed_network_label."""
    name = (network or _live_network_name() or "").strip().lower()
    if name == "mainnet":
        return "MainNet"
    if name == "testnet":
        return "TestNet"
    return "Algorand"


def _confirmed_network_label(conf: dict | None) -> str:
    """Label from independently confirmed fields only. Empty if unknown."""
    net = algo_anchor.confirmed_anchor_network(conf)
    if net == "mainnet":
        return "MainNet"
    if net == "testnet":
        return "TestNet"
    return ""


def _live_origin() -> str:
    from live402.pq import log_identity

    try:
        return log_identity.configured_origin()
    except Exception:
        return ORIGIN


def _confirmed_network(conf: dict | None = None) -> str:
    return algo_anchor.recorded_network_name(conf)


def _pera_address_base(network: str | None = None) -> str:
    name = (network or _live_network_name() or "").strip().lower()
    if name == "mainnet":
        return PERA_ADDRESS_URL_MAINNET
    if name == "testnet":
        return PERA_ADDRESS_URL
    return ""



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


def pera_tx_url(txid: str, network: str | None = None) -> str:
    """Confirmed-network explorer only. Missing network suppresses the link."""
    if not _looks_like_txid(txid):
        return ""
    net = (network or "").strip().lower()
    if not net:
        return ""
    return algo_anchor.verified_explorer_tx_url(txid.strip(), net)


def pera_address_url(address: str, network: str | None = None) -> str:
    """Account explorer for an explicit network, or configured identity."""
    text = str(address or "").strip()
    if not text or " " in text:
        return ""
    net = (network or "").strip().lower() or _live_network_name()
    return algo_anchor.verified_explorer_address_url(text, net)


def indexer_tx_url(txid: str, network: str | None = None) -> str:
    """Confirmed-network indexer JSON only. Missing network suppresses the link."""
    if not _looks_like_txid(txid):
        return ""
    net = (network or "").strip().lower()
    if not net:
        return ""
    return algo_anchor.verified_indexer_tx_url(txid.strip(), net)


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
    """Epoch env vkey wins over sqlite meta.vkey (SEC-ROUTER-004 / A-14).

    A stale persisted meta.vkey is not advertised or used to bind a
    checkpoint when trust.vkey() is set. Empty env still falls back to
    sqlite so in-process configure_signer tests keep working.
    """
    env = str(trust.vkey() or "").strip()
    if env:
        return env
    return str(store.meta_get("vkey") or "").strip()


def public_falcon_address() -> str:
    name = _live_network_name()
    if not name:
        return ""
    return str(algo_anchor.falcon_address_for(name) or "").strip()


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
    network = algo_anchor.confirmed_anchor_network(conf)
    explorer = pera_tx_url(txid, network)
    if explorer and not algo_anchor.explorer_hint_label(explorer):
        explorer = ""
    if network == "mainnet" and "testnet.explorer.perawallet.app" in explorer:
        explorer = ""
    return {
        "size": int(conf.get("size") or 0),
        "at": int(conf.get("at") or 0),
        "txid": txid,
        "round": int(conf.get("round") or 0),
        "root": root_hex(conf.get("root")),
        "origin": str(conf.get("origin") or ORIGIN),
        "network": network,
        "explorer": explorer,
        "indexer": indexer_tx_url(txid, network),
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
    send_state = str(auth.get("send_state") or "").strip()
    if send_state == "CONFIRMED":
        send_state = "SUBMITTED" if submitted else "AUTHORIZED"
    if send_state == "SEND_ATTEMPTED":
        status = "SEND_ATTEMPTED"
    elif submitted or send_state == "SUBMITTED":
        status = "SUBMITTED"
    else:
        status = "AUTHORIZED"
    labels = {
        "AUTHORIZED": "AUTHORIZED · awaiting %s confirmation" % _network_label(),
        "SEND_ATTEMPTED": "SEND_ATTEMPTED · awaiting %s confirmation" % _network_label(),
        "SUBMITTED": "SUBMITTED · awaiting %s confirmation" % _network_label(),
    }
    return {
        "size": size,
        "submitted": submitted,
        "status": status,
        "label": labels[status],
    }


def parse_checkpoint_fields(note: str) -> dict | None:
    """Parse a signed or unsigned checkpoint note. Size-labeling only.

    Never treat this as a successful bound proof. bound_checkpoint requires
    a trusted Ed25519 vkey and verify_signed_note.
    """
    if not note or not isinstance(note, str):
        return None
    try:
        signed = ckpt.parse_signed_note(note)
        body = ckpt.parse_checkpoint_body(signed["text"])
    except ValueError:
        try:
            body = ckpt.parse_checkpoint_body(note)
        except ValueError:
            return None
    root = body.get("root")
    if not isinstance(root, (bytes, bytearray)) or len(root) != 32:
        return None
    return {
        "origin": str(body.get("origin") or ""),
        "tree_size": int(body.get("tree_size") or 0),
        "root": bytes(root),
        "root_hex": bytes(root).hex(),
    }


def bound_checkpoint(conf: dict | None) -> dict | None:
    """Signed checkpoint for the confirmed tree only. Fail closed.

    Requires checkpoint_at(confirmed_size), a trusted Ed25519 vkey,
    verify_signed_note success, and origin / size / root match. Missing
    vkey, unsigned body, or a bad signature is not a bound checkpoint.
    """
    if not conf:
        return None
    size = int(conf.get("size") or 0)
    if size < 1:
        return None
    note = store.checkpoint_at(size)
    if not note:
        return None
    vkey = public_vkey()
    if not vkey:
        return None
    try:
        verified = ckpt.verify_signed_note(note, vkey)
    except ValueError:
        return None
    body = verified.get("body") if isinstance(verified, dict) else None
    if not isinstance(body, dict):
        return None
    root = body.get("root")
    if not isinstance(root, (bytes, bytearray)) or len(root) != 32:
        return None
    origin = str(body.get("origin") or "")
    tree_size = int(body.get("tree_size") or 0)
    root_hex = bytes(root).hex()
    want_origin = str(conf.get("origin") or ORIGIN)
    if origin != want_origin:
        return None
    if tree_size != size:
        return None
    if root_hex != str(conf.get("root") or ""):
        return None
    return {
        "size": size,
        "origin": origin,
        "root_hex": root_hex,
        "href": "/pq/log/checkpoint/%s" % size,
    }


def log_integrity_error(current: int, confirmed: dict | None) -> bool:
    if not confirmed:
        return False
    return current < int(confirmed.get("size") or 0)


def _history_row_ok(row: dict) -> dict | None:
    txid = str(row.get("txid") or "").strip()
    size = int(row.get("size") or 0)
    rnd = int(row.get("round") or 0)
    if size < 1 or rnd < 1 or not _looks_like_txid(txid):
        return None
    network = algo_anchor.confirmed_anchor_network(row)
    explorer = pera_tx_url(txid, network)
    if network == "mainnet" and "testnet.explorer.perawallet.app" in explorer:
        explorer = ""
    return {
        "size": size,
        "txid": txid,
        "round": rnd,
        "at": int(row.get("at") or 0),
        "utc": utc_text(int(row.get("at") or 0)),
        "iso": utc_iso(int(row.get("at") or 0)),
        "network": network,
        "explorer": explorer,
        "root": root_hex(row.get("root")),
    }


def history_rows() -> list[dict]:
    """Latest 250 confirmed anchors. Query 251 so the oldest visible delta
    can use a hidden baseline. The baseline row is never rendered.
    """
    fetched = store.list_confirmed_anchors(limit=HISTORY_LIMIT + 1)
    hidden = fetched[HISTORY_LIMIT] if len(fetched) > HISTORY_LIMIT else None
    visible_src = fetched[:HISTORY_LIMIT]
    valid = []
    for row in visible_src:
        parsed = _history_row_ok(row)
        if parsed:
            valid.append(parsed)
    baseline = 0
    if hidden:
        hidden_ok = _history_row_ok(hidden)
        if hidden_ok:
            baseline = int(hidden_ok["size"])
    chronological = sorted(valid, key=lambda r: (r["at"], r["size"]))
    prev = baseline
    deltas = {}
    spans = {}
    for row in chronological:
        size = row["size"]
        if prev <= 0:
            deltas[id(row)] = size
            spans[id(row)] = "leaves 1–%s" % size if size >= 1 else ""
        else:
            deltas[id(row)] = size - prev
            if size > prev:
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
    inconsistent = log_integrity_error(current, confirmed)
    if inconsistent:
        growth = 0
        caught_up = False
    else:
        growth = current - confirmed_size if current >= confirmed_size else 0
        caught_up = bool(confirmed) and growth == 0
    history = history_rows()
    total = store.confirmed_anchor_count()
    note = note_from_confirmed(confirmed) if confirmed else None
    bound = bound_checkpoint(confirmed) if confirmed and not inconsistent else None
    latest_fields = parse_checkpoint_fields(str(store.latest_checkpoint() or ""))
    return {
        "current_size": current,
        "confirmed": confirmed,
        "confirmed_size": confirmed_size,
        "growth": growth,
        "caught_up": caught_up,
        "integrity_error": inconsistent,
        "lifecycle": authorized_lifecycle(),
        "history": history,
        "anchors_confirmed": total,
        "history_truncated": total > len(history),
        "note": note,
        "bound_checkpoint": bound,
        "latest_checkpoint_size": int(latest_fields["tree_size"]) if latest_fields else 0,
        "vkey": public_vkey(),
        "falcon_address": public_falcon_address(),
    }


HOMEPAGE_AWAITING_CHIP = (
    '<p class="pq-chip"><!--PQ_LATEST-->Awaiting checkpoint</p>'
)
HOMEPAGE_ANCHORED_CHIP = '<p class="pq-chip is-anchored">Anchored</p>'


def homepage_pq_html() -> str:
    """Homepage status chip. Anchored only when last_confirmed has a real txid."""
    conf = confirmed_view()
    if not conf:
        return ""
    if log_integrity_error(int(store.size() or 0), conf):
        return ""
    return HOMEPAGE_ANCHORED_CHIP


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


def _ext_link(href: str, label: str, network: str | None = None) -> str:
    hint = algo_anchor.explorer_hint_label(href)
    if not hint and network:
        labeled = _network_label(network)
        if labeled in {"MainNet", "TestNet"}:
            hint = labeled
    if not href or not hint:
        return ""
    return (
        '<a href="%s" rel="noopener noreferrer">%s '
        '<span class="ext-hint">(Pera Explorer, %s)</span></a>'
        % (esc(href), esc(label), esc(hint))
    )


def _integrity_banner(model: dict) -> str:
    if not model.get("integrity_error"):
        return ""
    return (
        '<section class="block" id="log-integrity" role="alert">\n'
        "  <h2>LOCAL LOG INCONSISTENT</h2>\n"
        "  <p>The local transparency log is smaller than its latest confirmed historical checkpoint.</p>\n"
        "</section>\n"
    )


def _status_strip(model: dict) -> str:
    confirmed = model["confirmed"]
    current = model["current_size"]
    confirmed_size = model["confirmed_size"]
    growth = model["growth"]
    n_anchors = model["anchors_confirmed"]
    if model.get("integrity_error"):
        status = "Local log inconsistent"
    elif confirmed and growth == 0:
        status = "Caught up"
    elif confirmed and growth > 0:
        status = "%s newer entries" % growth
    else:
        status = "Not anchored yet"
    latest = _time(confirmed["utc"], confirmed["iso"]) if confirmed else "-"
    last_anchored = str(confirmed_size) if confirmed else "-"
    return (
        '<section class="status-grid pq-status" aria-label="Current status">\n'
        '  <div><p class="pq-kicker">LOG SIZE</p><p class="pq-stat">%s</p></div>\n'
        '  <div><p class="pq-kicker">LAST ANCHORED</p><p class="pq-stat">%s</p></div>\n'
        '  <div><p class="pq-kicker">STATUS</p><p class="pq-stat">%s</p></div>\n'
        '  <div><p class="pq-kicker">ANCHORS</p><p class="pq-stat">%s</p></div>\n'
        '  <div><p class="pq-kicker">VERIFIED</p><p class="pq-stat">%s</p></div>\n'
        "</section>\n"
        % (esc(current), esc(last_anchored), esc(status), esc(n_anchors), latest)
    )




def _pera_views(model: dict) -> str:
    conf = model["confirmed"]
    addr = model["falcon_address"]
    items = []
    if conf and conf.get("explorer"):
        link = _ext_link(conf["explorer"], "View latest anchor on Pera", conf.get("network"))
        if link:
            items.append("<li>%s</li>" % link)
    net = _confirmed_network(conf) if conf else _live_network_name()
    account = pera_address_url(addr, net)
    if account:
        link = _ext_link(account, "View Falcon account on Pera", net)
        if link:
            items.append(
                "<li>%s. This is the configured Falcon-1024 account. "
                "Not every transaction on that account is a valid 402Signal checkpoint "
                "without PQ1 verification. First-party confirmed history is listed below.</li>"
                % link
            )
    if not items:
        return ""
    heading = "Explorers"
    conf_label = _confirmed_network_label(conf) if conf else ""
    if conf_label:
        heading = "%s explorers" % conf_label
    return (
        '<section class="block" id="pera-views">\n'
        "  <h2>%s</h2>\n"
        '  <ul class="verify-list">%s</ul>\n'
        "</section>\n"
        % (esc(heading), "".join(items))
    )


def _confirmed_card(model: dict) -> str:
    conf = model["confirmed"]
    if not conf:
        return (
            '<section class="block" id="latest-confirmed">\n'
            "  <h2>Latest confirmed checkpoint</h2>\n"
            "  <p>Awaiting first confirmed MainNet checkpoint.</p>\n"
            "</section>\n"
        )
    cta = _confirmed_checkpoint_cta(model)
    bind_note = ""
    if not model.get("bound_checkpoint"):
        bind_note = (
            '    <p class="note">The signed checkpoint for this confirmed tree could not be bound '
            "to the confirmed origin, tree size, and Merkle root.</p>\n"
        )
    pera = ""
    if conf.get("explorer") and algo_anchor.explorer_hint_label(conf["explorer"]):
        pera = (
            '      <a class="btn" href="%s" rel="noopener noreferrer">View latest anchor on Pera</a>\n'
            % esc(conf["explorer"])
        )
    actions = ""
    if pera or cta:
        actions = '    <div class="hero-actions">\n%s%s    </div>\n' % (pera, cta)
    return (
        '<section class="block" id="latest-confirmed">\n'
        "  <h2>Latest confirmed checkpoint</h2>\n"
        '  <article class="panel confirm-card">\n'
        '    <p class="pq-kicker">STATUS</p>\n'
        "    <p>Confirmed</p>\n"
        '    <p class="pq-kicker">TREE SIZE</p>\n'
        "    <p>%s</p>\n"
        '    <p class="pq-kicker">BLOCK</p>\n'
        "    <p>%s</p>\n"
        '    <p class="pq-kicker">VERIFIED AT</p>\n'
        "    <p>%s</p>\n"
        '    <p class="pq-kicker">TRANSACTION</p>\n'
        "    <p>%s</p>\n"
        '    <p class="pq-kicker">AUTHORIZATION</p>\n'
        "    <p>Falcon-1024 (f1)</p>\n"
        "%s"
        "%s"
        "  </article>\n"
        "</section>\n"
        % (
            esc(conf["size"]),
            esc(conf["round"]),
            _time(conf["utc"], conf["iso"]),
            _mono_copy(conf["txid"], abbreviate(conf["txid"]), "transaction id"),
            actions,
            bind_note,
        )
    )


def _confirmed_checkpoint_cta(model: dict) -> str:
    bound = model.get("bound_checkpoint")
    if bound and bound.get("href"):
        return (
            '      <a class="btn secondary" href="%s">View signed checkpoint for tree %s</a>\n'
            % (esc(bound["href"]), esc(bound["size"]))
        )
    return ""


def _confirmed_detail_fields(model: dict) -> str:
    conf = model.get("confirmed")
    if not conf:
        return ""
    falcon = ""
    addr = str(model.get("falcon_address") or "").strip()
    if addr:
        falcon = (
            "  <div><dt>FALCON ACCOUNT</dt><dd>%s</dd></div>\n"
            % _mono_copy(addr, abbreviate_falcon(addr), "Falcon account")
        )
    origin = esc(str(conf.get("origin") or _live_origin()))
    return (
        "<h3>Confirmed checkpoint fields</h3>\n"
        '<dl class="pq-decode">\n'
        "  <div><dt>MERKLE ROOT</dt><dd>%s</dd></div>\n"
        "  <div><dt>ORIGIN</dt><dd>%s</dd></div>\n"
        "%s"
        "  <div><dt>AMOUNT</dt><dd>0 ALGO</dd></div>\n"
        "</dl>\n"
        % (
            _mono_copy(conf["root"], None, "Merkle root"),
            origin,
            falcon,
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


def _pq1_decoder(note: dict, network_label: str = "") -> str:
    match = "matches note origin-hash bytes" if note["origin_hash_matches"] else "does not match"
    net_word = (" " + network_label) if network_label else ""
    return (
        "<p>Canonical PQ1 note. Reconstructed from the fields independently verified "
        "against the confirmed%s transaction.</p>\n"
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
            esc(net_word),
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
    if model.get("caught_up") and not model.get("integrity_error"):
        return ""
    current = model["current_size"]
    confirmed_size = model["confirmed_size"]
    growth = model["growth"]
    if model.get("integrity_error"):
        compare = (
            "The local transparency log is smaller than its latest confirmed historical checkpoint."
        )
        numbers = (
            "Current tree %s · confirmed tree %s. Local log is inconsistent with the "
            "confirmed historical checkpoint." % (esc(current), esc(confirmed_size))
        )
        lifecycle = ""
    else:
        if model["confirmed"] and growth == 0:
            compare = "The latest log checkpoint is anchored."
        elif model["confirmed"] and growth > 0:
            compare = "%s newer log entries exist after the latest confirmed anchor." % growth
        elif current > 0 and not model["confirmed"]:
            compare = "The log has entries. Awaiting first confirmed MainNet checkpoint."
        else:
            compare = "Awaiting first confirmed MainNet checkpoint."
        numbers = "Current tree %s · confirmed tree %s · unanchored growth %s." % (
            esc(current),
            esc(confirmed_size if model["confirmed"] else 0),
            esc(growth),
        )
        lifecycle = ""
        if model["lifecycle"]:
            lifecycle = "<p>%s</p>\n" % esc(model["lifecycle"]["label"])
    return (
        '<section class="block" id="current-vs-anchored">\n'
        "  <h2>Current vs anchored</h2>\n"
        "  <p>%s</p>\n"
        "  <p>%s</p>\n"
        "%s"
        "</section>\n"
        % (
            numbers,
            esc(compare),
            lifecycle,
        )
    )


def _history(model: dict) -> str:
    rows = model["history"]
    n = model["anchors_confirmed"]
    heading = "<p>TOTAL CONFIRMED ANCHORS %s</p>\n" % esc(n)
    if model.get("history_truncated"):
        heading += "<p>Showing latest 250 anchors.</p>\n"
    if not rows:
        return (
            '<section class="block" id="anchor-history">\n'
            "  <h2>Confirmed anchor history</h2>\n"
            + heading
            + "</section>\n"
        )
    cards = []
    body = []
    for row in rows:
        span = (" · %s" % row["span"]) if row.get("span") else ""
        when = _time(row["utc"], row["iso"])
        link = _ext_link(row.get("explorer") or "", abbreviate(row["txid"]), row.get("network"))
        if not link:
            link = esc(abbreviate(row["txid"]))
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
            "<p>TREE %s%s</p><p>Δ LEAVES %s</p><p>VERIFIED %s</p>"
            "<p>BLOCK %s</p><p>TRANSACTION %s</p></article>"
            % (esc(row["size"]), esc(span), esc(row["delta"]), when, esc(row["round"]), link)
        )
    return (
        '<section class="block" id="anchor-history">\n'
        "  <h2>Confirmed anchor history</h2>\n"
        + heading
        + '  <div class="table-wrap history-table desktop-only">\n'
        '    <table class="anchor-table">\n'
        "      <thead><tr><th scope=\"col\">TREE</th><th scope=\"col\">Δ LEAVES</th>"
        "<th scope=\"col\">VERIFIED</th><th scope=\"col\">BLOCK</th>"
        "<th scope=\"col\">TRANSACTION</th></tr></thead>\n"
        "      <tbody>%s</tbody>\n"
        "    </table>\n"
        "  </div>\n"
        '  <div class="history-cards mobile-only">%s</div>\n'
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
    networks = {str(r.get("network") or "") for r in rows}
    if networks == {"testnet"}:
        label = "TestNet"
    elif networks == {"mainnet"}:
        label = "MainNet"
    else:
        label = ""
    caption_net = ("%s " % label) if label else ""
    return (
        '<figure class="growth-chart">\n'
        "  <figcaption>Tree size at each time 402Signal verified a confirmed "
        "%sanchor. The line joins those observations only.</figcaption>\n"
        '  <svg viewBox="0 0 %s %s" role="img" '
        'aria-label="Tree size at each time 402Signal verified a confirmed %sanchor. '
        'Points are real confirmed checkpoints. The line joins those observations.">\n'
        '    <polyline fill="none" stroke="#e49c60" stroke-width="1.5" points="%s" />\n'
        "    %s\n"
        "  </svg>\n"
        "</figure>\n"
        % (caption_net, width, height, caption_net, " ".join(points), "".join(circles))
    )


def _technical(model: dict) -> str:
    conf = model["confirmed"]
    note = model["note"]
    parts = []
    parts.append(
        "<p>Authorization · Falcon-1024 · f1 as native Algorand PQ tx auth "
        "for the checkpoint</p>"
    )
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
    bound = model.get("bound_checkpoint")
    if bound and bound.get("href"):
        parts.append(
            '<p>Signed checkpoint for confirmed tree %s: '
            '<a href="%s"><code>%s</code></a></p>'
            % (esc(bound["size"]), esc(bound["href"]), esc(bound["href"]))
        )
    elif model.get("confirmed") and not model.get("integrity_error"):
        parts.append(
            "<p>The signed checkpoint for this confirmed tree could not be bound "
            "to the confirmed origin, tree size, and Merkle root.</p>"
        )
    latest_label = _confirmed_network_label(conf) or _network_label()
    parts.append(
        '<p>Latest signed checkpoint. It may be newer than the latest %s anchor. '
        '<a href="/pq/log/checkpoint"><code>GET /pq/log/checkpoint</code></a></p>'
        % latest_label
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
        indexer = str(conf.get("indexer") or "")
        indexer_hint = algo_anchor.explorer_hint_label(indexer)
        if indexer and indexer_hint:
            parts.append(
                '<p><a href="%s" rel="noopener noreferrer">View raw %s transaction JSON</a> '
                '<span class="ext-hint">(Algonode %s indexer)</span></p>'
                % (esc(indexer), esc(indexer_hint), esc(indexer_hint))
            )
    body = "\n".join(parts) if parts else "<p>No additional public fields yet.</p>"
    return "<h3>Technical details</h3>\n<div class=\"tech-body\">%s</div>\n" % body


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
        copies.append(("Merkle root", conf["root"], abbreviate(conf["root"]), "Merkle root"))
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
    bound = model.get("bound_checkpoint")
    bound_item = ""
    if bound and bound.get("href"):
        bound_item = (
            '    <li><a href="%s">GET %s</a> '
            "(signed checkpoint for confirmed tree %s)</li>\n"
            % (esc(bound["href"]), esc(bound["href"]), esc(bound["size"]))
        )
    return (
        '<section class="block" id="verify-yourself">\n'
        "  <h2>Verify yourself</h2>\n"
        "  <ul class=\"verify-list\">\n"
        + bound_item
        + (
            '    <li><a href="/pq/log/checkpoint">GET /pq/log/checkpoint</a>. '
            "Latest signed checkpoint. It may be newer than the latest %s anchor.</li>\n"
            % (_confirmed_network_label(conf) or _network_label())
        )
        + "    <li>C2SP tiles are published under <code>/pq/log/tile/</code> "
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
    return site_chrome.header_html()


def _site_footer(*, current: str = "") -> str:
    return site_chrome.footer_html(current=current)


def render_html() -> str:
    return page_html()


def page_html() -> str:
    model = page_model()
    title = "402Signal transparency log"
    description = (
        "Verify 402Signal's PQ Trust append-only routing-evidence log. "
        "Production log identity is Algorand MainNet. "
        "Awaiting first confirmed MainNet checkpoint."
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


def _verification_details(model: dict) -> str:
    note = model.get("note")
    decoder = ""
    if model.get("confirmed"):
        if note:
            decoder = (
                '<div class="pq-decoder">\n'
                "  <h3>What the transaction commits</h3>\n"
                "  <p>Generic explorers may show unreadable text. 402Signal decodes the same "
                "84-byte PQ1 layout.</p>\n"
                "%s"
                "</div>\n" % _pq1_decoder(note, _confirmed_network_label(model.get("confirmed")))
            )
        else:
            decoder = (
                "<p>The committed note could not be reconstructed from verified fields.</p>\n"
            )
    return (
        '<details class="tech-details" id="verification-details">\n'
        "  <summary>Verification details</summary>\n"
        '  <div class="tech-body">\n'
        + _confirmed_detail_fields(model)
        + decoder
        + _pera_views(model)
        + _technical(model)
        + _verify_yourself(model)
        + "  </div>\n"
        "</details>\n"
    )


def _mainnet_confirmed(model: dict) -> bool:
    """True only when last_confirmed is independently verified as MainNet."""
    conf = model.get("confirmed")
    return bool(conf) and _confirmed_network_label(conf) == "MainNet"


def _hero_badge(model: dict) -> str:
    if _mainnet_confirmed(model):
        return '        <p class="pq-badge">Algorand MainNet log</p>\n'
    return (
        '        <p class="pq-badge">Algorand MainNet log · awaiting first '
        "confirmed checkpoint</p>\n"
    )


def _hero_lede(model: dict) -> str:
    if _mainnet_confirmed(model):
        return (
            "        <p class=\"lede\">PQ Trust is 402Signal's append-only transparency layer. "
            "Confirmed checkpoints are independently anchored to "
            "Algorand MainNet using native Falcon-1024 post-quantum authorization.</p>\n"
        )
    return (
        "        <p class=\"lede\">PQ Trust is 402Signal's append-only transparency layer. "
        "Production log identity is Algorand MainNet. "
        "Awaiting first confirmed MainNet checkpoint. Checkpoint transactions use native "
        "Falcon-1024 post-quantum authorization when confirmed on MainNet.</p>\n"
    )


def _main(model: dict) -> str:
    return (
        '      <section class="hero compact">\n'
        + _hero_badge(model)
        + "        <h1>Verify the transparency log</h1>\n"
        + _hero_lede(model)
        + "        <p class=\"note\">Routing does not wait for confirmation. "
        "Paid HTTP 200 or 503 does not require a durable signed leaf unless "
        "require_transparency is set.</p>\n"
        + "        <p class=\"privacy-note\">Public transparency commitments do not expose raw "
        "needs, wallets, payment signatures, or seller response bodies.</p>\n"
        '        <details class="tech-details" id="what-is-published">\n'
        "          <summary>What is published?</summary>\n"
        '          <div class="tech-body">\n'
        "            <p>This page publishes 402Signal infrastructure commitments: log size, "
        "signed checkpoints, and confirmed MainNet anchors when present. It does not publish agent "
        "needs, wallets, payment signatures, seller response bodies, raw requests, or "
        "payment credentials. A v2 public leaf includes type, timestamp, nonce, "
        "commitment hash, and optional live/miss_reason. It does not include salt, "
        "raw evidence, need, wallet, or payment. Published fields are not a claim of "
        "anonymous, unlinkable, or fully private traffic.</p>\n"
        "          </div>\n"
        "        </details>\n"
        "      </section>\n"
        + _integrity_banner(model)
        + "      <h2>Current status</h2>\n"
        + _status_strip(model)
        + _confirmed_card(model)
        + _current_vs_anchored(model)
        + _history(model)
        + _verification_details(model)
        + '      <section class="block">\n'
        + "        <h2>Historical TestNet archive</h2>\n"
        + "        <p>The prior public TestNet log shard remains available for reference. "
        + "It is not the live production MainNet log.</p>\n"
        + "      </section>\n"
        + '      <section class="block">\n'
        + "        <h2>What this proves / does not prove</h2>\n"
        + "        <p>Anyone can compare the signed checkpoint, Merkle root, and confirmed "
        + "MainNet transaction. Later rewriting inconsistent with published checkpoints "
        + "becomes detectable. Detectability is not a claim that the log cannot be "
        + "rewritten; it is a claim that inconsistent rewriting can be noticed.</p>\n"
        + "        <p>This page reports 402Signal's committed routing-evidence history. "
        + "It does not report whether a seller endpoint described its service accurately. "
        + "The Algorand transaction authorizes a checkpoint. It is not a merchant payment. "
        + "This post-quantum authorization protects the checkpoint transaction. "
        + "It does not make Base or Solana merchant payments post-quantum secure. "
        + "Routing does not wait for confirmation. The caller retains custody of keys, "
        + "signing, and the selected service's payment.</p>\n"
        + "      </section>\n"
    )


def assert_no_secrets(html: str) -> None:
    low = html.lower()
    for marker in _SECRET_MARKERS:
        if marker.lower() in low:
            raise ValueError("secret marker in html")
    return
