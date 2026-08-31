"""Public x402 listing pulse. Allowlisted catalogs only. Never fetches caller URLs."""

from __future__ import annotations

import html
import json
import re
import statistics
import threading
import time
from urllib.parse import urlparse, urlencode

from live402 import catalog, fixtures, payment, probe

CACHE_TTL = 15.0
OURS_URL = "https://402signal.com/route"
OURS_NAME = "402Signal"
CHAINS = ("base", "solana", "algorand")
CHAIN_LABELS = {
    "base": "Base",
    "solana": "Solana",
    "algorand": "Algorand",
}

# Fixed taxonomy so charts are comparable across chains. Leftover → other.
THEME_ORDER = (
    "search",
    "onchain",
    "market",
    "nft",
    "identity",
    "weather",
    "compute",
    "messaging",
    "payments",
    "storage",
    "games",
    "other",
)
THEME_LABELS = {
    "search": "search",
    "onchain": "on-chain read",
    "market": "market/price",
    "nft": "nft",
    "identity": "identity/auth",
    "weather": "weather",
    "compute": "compute/ai",
    "messaging": "messaging",
    "payments": "payments",
    "storage": "storage",
    "games": "games",
    "other": "other",
}
# First match wins. Do not put x402/usdc here — every listing would be "payments".
# Do not theme by chain rail names (solana/algorand/base) — every listing would match.
THEME_KEYWORDS = (
    ("weather", {"weather", "forecast", "climate", "temperature", "meteo"}),
    ("nft", {"nft", "nfts", "collectible", "opensea", "metadata", "erc721", "erc1155"}),
    ("identity", {"identity", "auth", "kyc", "login", "did", "siwe", "oauth", "signin"}),
    ("games", {"game", "games", "chess", "coinflip", "casino", "bet", "wager", "play"}),
    ("storage", {"upload", "file", "files", "blob", "s3", "ipfs", "store", "kv", "bucket"}),
    ("search", {"search", "google", "query", "find", "lookup", "websearch", "browse", "serp"}),
    ("compute", {"ai", "llm", "grok", "gpt", "compute", "inference", "model", "generate", "openai", "claude", "agent", "agents"}),
    ("messaging", {"message", "email", "sms", "notify", "slack", "telegram", "webhook", "inbox", "inboxes", "thread", "threads", "dm", "mail"}),
    ("payments", {"invoice", "checkout", "payout", "billing", "merchant", "payroll", "remittance", "card", "cards", "giftcard"}),
    ("market", {"price", "market", "ticker", "quote", "floor", "trading", "swap", "dex", "candle", "ohlc", "ohlcv", "defi", "tvl", "yield", "volume", "liquidity", "news"}),
    ("onchain", {"erc20", "token", "balance", "gas", "chain", "blockchain", "contract", "wallet", "address", "transaction", "ethereum", "onchain", "block", "rpc", "abi", "allowance"}),
)
# Hostname labels glue distinctive words (onestepchess, coinflip402). Keep this short.
_HOST_GLUE = ("chess", "coinflip", "casino", "upload", "giftcard")

PREFERRED_SAMPLE_THEMES = ("weather", "onchain", "search", "market", "messaging", "storage")
DEFER_SAMPLE_THEMES = frozenset({"games", "other"})
MAX_SAMPLES = 4
NEED_MAX = 40
PREVIEW_DISPLAY = 8
_PATH_SKIP = frozenset({
    "api", "v0", "v1", "v2", "v3", "v4", "http", "https", "www", "com",
    "index", "json", "xml", "html", "x402", "mcp", "rest", "public",
})
_PATH_GENERIC = frozenset({
    "chain", "chains", "base", "solana", "algorand", "ethereum",
    "mainnet", "testnet",
})
_NEED_SKIP = frozenset({
    "fixture", "stale", "probe", "local", "test", "demo", "example",
    "null", "none", "undefined",
})
_NEED_EXPAND = {
    "erc20": ("erc20", "token"),
    "inboxes": ("inbox",),
}
_DEFER_NEED_WORDS = frozenset({
    "riddle", "riddles", "fortune", "fortunes", "game", "games",
    "coinflip", "chess", "casino",
})
_THEME_HINTS = {
    "weather": ("weather", "climate", "temperature", "meteo"),
    "onchain": ("erc20", "balance", "gas", "block", "token", "contract", "wallet", "transaction", "onchain"),
    "search": ("search", "google", "lookup", "serp", "websearch", "browse"),
    "market": ("price", "market", "ticker", "quote", "ohlc", "defi", "tvl"),
    "messaging": ("message", "inbox", "email", "sms", "slack", "telegram"),
    "storage": ("upload", "file", "ipfs", "blob", "store", "bucket"),
}
_WALLET_RE = re.compile(
    r"^(?:0x[0-9a-fA-F]{8,}|[A-Z2-7]{58}|[1-9A-HJ-NP-Za-km-z]{32,})$"
)

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "payload": None}
# Last good per-chain snapshot so a failed fetch shows stale, not a blank freeze.
_last_good: dict[str, dict] = {}
_last_good_at: dict[str, float] = {}
_collecting = False


def reset_cache() -> None:
    global _collecting
    with _lock:
        _cache["at"] = 0.0
        _cache["payload"] = None
        _collecting = False
        _last_good.clear()
        _last_good_at.clear()


def usdc_atomic_to_price(amount) -> tuple[str, float | None]:
    """USDC 6 decimals: 10000 -> $0.01. Never treat atomic as dollars."""
    label, usd = payment.usdc_from_atomic(amount)
    if label is None and usd is None:
        return "unknown", None
    return label or "unknown", usd


def _listing_name(item: dict, url: str) -> str:
    for key in ("serviceName", "toolName"):
        val = item.get(key)
        if val and str(val).strip() and str(val).strip().lower() not in {"null", "none"}:
            return str(val).strip()[:80]
    desc = str(item.get("description") or "").strip()
    if desc.lower().startswith("service:"):
        desc = desc[8:].strip()
    if " (" in desc and desc.endswith(")"):
        head = desc.split(" (", 1)[0].strip()
        if head:
            desc = head
    if desc:
        return desc[:80]
    host = urlparse(url).hostname or url
    if host.endswith("402signal.com"):
        return OURS_NAME
    return host[:80]


def _accepts(item: dict) -> list[dict]:
    raw = item.get("accepts") or []
    return [a for a in raw if isinstance(a, dict)]


def _price_from_accept(acc: dict) -> tuple[str, float | None]:
    opt = payment.payment_option_from_accept(acc)
    if not opt:
        return "unknown", None
    if opt.get("normalized_usd") is not None:
        return opt.get("display_amount") or "unknown", opt["normalized_usd"]
    if opt.get("display_amount"):
        return opt["display_amount"], None
    return "unknown", None


def _is_ours(url: str) -> bool:
    u = (url or "").strip().lower().rstrip("/")
    return u == OURS_URL or u.endswith("402signal.com/route")


def _item_price_usd(item: dict) -> float | None:
    accepts = _accepts(item)
    if accepts:
        _label, usd = _price_from_accept(accepts[0])
        return usd
    return None


def _item_chains(item: dict, fallback: str) -> list[str]:
    rails: list[str] = []
    for acc in _accepts(item):
        rail = payment.rail_of_network(acc.get("network") or "")
        if rail in CHAINS and rail not in rails:
            rails.append(rail)
    if rails:
        return rails
    rail = fallback if fallback in CHAINS else probe._item_rail(item)
    if rail not in CHAINS:
        rail = "base"
    return [rail]


def _stem_lite(toks: set[str]) -> set[str]:
    """messages→message, agents→agent. Skip short tokens (gas, ens, news)."""
    extra = {t[:-1] for t in toks if len(t) > 4 and t.endswith("s")}
    return toks | extra


# probe._tokens drops len<3. Only keep 2-letter tokens that are real keywords.
# Do not ingest TLDs (*.ai would otherwise become compute).
_SHORT_KEYS = {"s3", "kv", "dm", "ai"}


def _url_theme_tokens(url: str) -> set[str]:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    labels = [p for p in host.split(".") if p]
    host_blob = " ".join(labels[:-1] if len(labels) > 1 else labels)
    blob = f"{host_blob} {path}"
    toks = probe._tokens(blob)
    buf: list[str] = []
    for ch in blob:
        if ch.isalnum():
            buf.append(ch)
        else:
            if buf:
                tok = "".join(buf)
                if tok in _SHORT_KEYS:
                    toks.add(tok)
                buf = []
    if buf:
        tok = "".join(buf)
        if tok in _SHORT_KEYS:
            toks.add(tok)
    for label in labels:
        for hint in _HOST_GLUE:
            if hint in label:
                toks.add(hint)
    return toks


def theme_id_for(item: dict, url: str) -> str:
    name = _listing_name(item, url)
    desc = str(item.get("description") or "")
    tags = item.get("tags") or []
    tag_text = " ".join(str(t) for t in tags) if isinstance(tags, list) else str(tags)
    bazaar = ((item.get("extensions") or {}).get("bazaar") or {})
    info = bazaar.get("info") or {} if isinstance(bazaar, dict) else {}
    tool = str((info.get("input") or {}).get("toolName") or "") if isinstance(info, dict) else ""
    blob = " ".join([name, desc, tag_text, tool])
    toks = _stem_lite(probe._tokens(blob) | _url_theme_tokens(url))
    for theme_id, keywords in THEME_KEYWORDS:
        if toks & keywords:
            return theme_id
    # mcp alone is weak: only if the path has it and nothing else matched.
    path_toks = _stem_lite(probe._tokens(urlparse(url or "").path or ""))
    if "mcp" in path_toks:
        return "compute"
    return "other"


def _clip_need(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) <= NEED_MAX:
        return text
    clipped = text[:NEED_MAX].rsplit(" ", 1)[0].strip()
    return clipped or text[:NEED_MAX].strip()


def _looks_like_wallet(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    return bool(_WALLET_RE.match(raw))


def _is_bad_need(need: str, url: str) -> bool:
    n = (need or "").strip().lower()
    if not n:
        return True
    host = (urlparse(url).hostname or "").lower()
    if n == host or n == (host.split(".")[0] if host else ""):
        return True
    if "." in n and " " not in n:
        return True
    compact = n.replace(" ", "")
    if _looks_like_wallet(compact) or _looks_like_wallet(n):
        return True
    return False


def _need_from_path(url: str) -> str:
    parsed = urlparse(url or "")
    parts = [p for p in (parsed.path or "").split("/") if p]
    toks: list[str] = []
    for part in parts:
        raw = part.strip()
        if not raw or raw.startswith(":") or raw.startswith("{") or raw.startswith("<"):
            continue
        raw = raw.split(".")[0]
        low = raw.lower()
        if re.fullmatch(r"v\d+", low) or low in _PATH_SKIP or low in probe.STOP:
            continue
        if _looks_like_wallet(raw):
            continue
        for piece in re.split(r"[-_]+", raw):
            piece = piece.lower()
            if not piece or piece in _PATH_SKIP or piece in probe.STOP or piece in _NEED_SKIP:
                continue
            if re.fullmatch(r"v\d+", piece) or piece in {"id", "ids"}:
                continue
            if _looks_like_wallet(piece):
                continue
            if piece in _NEED_EXPAND:
                for extra in _NEED_EXPAND[piece]:
                    if extra not in toks:
                        toks.append(extra)
            elif piece not in toks:
                toks.append(piece)
    if len(toks) > 1:
        trimmed = [x for x in toks if x not in _PATH_GENERIC]
        if trimmed:
            toks = trimmed
    return _clip_need(" ".join(toks))


def _need_from_description(item: dict, url: str) -> str:
    desc = str(item.get("description") or "").strip()
    if desc.lower().startswith("service:"):
        desc = desc[8:].strip()
    if " (" in desc and desc.endswith(")"):
        head = desc.split(" (", 1)[0].strip()
        if head:
            desc = head
    cut = len(desc)
    for sep in (". ", ".\n", "! ", "? ", "\n"):
        i = desc.find(sep)
        if i >= 0:
            cut = min(cut, i)
    desc = desc[:cut].strip()
    host = (urlparse(url).hostname or "").lower()
    words: list[str] = []
    for raw in re.split(r"\s+", desc):
        cleaned = re.sub(r"[^A-Za-z0-9]+", "", raw)
        low = cleaned.lower()
        if not low or low in probe.STOP or low in _NEED_SKIP:
            continue
        if _looks_like_wallet(cleaned) or _looks_like_wallet(raw):
            continue
        if host and (low == host or low == host.split(".")[0]):
            continue
        words.append(low)
        if len(" ".join(words)) >= NEED_MAX:
            break
    return _clip_need(" ".join(words))


def sample_need_for(item: dict, url: str) -> str | None:
    """Short human lookup string. Path tokens first, then description. Never a hostname."""
    need = _need_from_path(url) or _need_from_description(item, url)
    if _is_bad_need(need, url):
        return None
    return _clip_need(need)


def named_chain(need: str) -> str | None:
    """If the caller names exactly one of base/solana/algorand, keep that chain.

    Chain-ambiguous (none or more than one named) → None. Token match only so
    'database' does not count as Base.
    """
    raw = (need or "").strip().lower()
    if not raw:
        return None
    toks = probe._tokens(raw)
    for piece in raw.replace("/", " ").replace("-", " ").replace(",", " ").split():
        if piece:
            toks.add(piece)
    found = [c for c in CHAINS if c in toks]
    if len(found) == 1:
        return found[0]
    return None


def _mixed_samples(chains: dict) -> list[dict]:
    """Homepage chips: real catalog URLs in CHAINS order. No rail bonus. Never invents URLs."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for chain in CHAINS:
        for sample in list((chains.get(chain) or {}).get("samples") or []):
            if not isinstance(sample, dict):
                continue
            url = str(sample.get("url") or "").strip()
            need = str(sample.get("need") or "").strip()
            if not url:
                continue
            key = (need.lower(), url)
            if key in seen:
                continue
            seen.add(key)
            out.append(sample)
    return out


def _item_facilitator(item: dict) -> str | None:
    """Echo facilitator URL from accepts extra. Never invent x402.org."""
    for acc in _accepts(item):
        extra = acc.get("extra") if isinstance(acc.get("extra"), dict) else {}
        raw = extra.get("facilitator")
        url = None
        if isinstance(raw, str) and raw.strip().startswith("https://"):
            url = raw.strip()
        elif isinstance(raw, dict):
            cand = str(raw.get("url") or "").strip()
            if cand.startswith("https://"):
                url = cand
        if url:
            return url
    return None


def _rails_up_map() -> dict[str, bool]:
    try:
        from live402 import rails as rails_mod
        data = rails_mod.get_rails()
    except Exception:
        return {}
    out: dict[str, bool] = {}
    for row in data.get("rails") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("network") or "")
        if name:
            out[name] = bool(row.get("up"))
    return out


def _item_price_label(item: dict) -> str:
    accepts = _accepts(item)
    if accepts:
        label, _usd = _price_from_accept(accepts[0])
        return label
    return "unknown"


def _sample_href(url: str) -> str | None:
    href = _https_href(url)
    if href:
        return href
    if fixtures.fixture_mode() and str(url or "").startswith("https://"):
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.netloc and not parsed.username:
            return url
    return None


def _deferred_need(need: str) -> bool:
    toks = set((need or "").lower().split())
    return bool(toks & _DEFER_NEED_WORDS)


def _samples_for_items(chain: str, items: list[dict]) -> list[dict]:
    """Up to 4 sample lookups per chain from the same catalog items. Never fetches URLs."""
    preferred_by_theme: dict[str, list[dict]] = {tid: [] for tid in PREFERRED_SAMPLE_THEMES}
    extra: list[dict] = []
    deferred: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = probe._resource_url(item)
        href = _sample_href(url)
        if not href:
            continue
        if probe.skip_candidate_url(url):
            continue
        if _is_ours(url):
            continue
        need = sample_need_for(item, url)
        if not need:
            continue
        tid = theme_id_for(item, url)
        sample = {
            "need": need,
            "label": need,
            "url": href,
            "price": _item_price_label(item),
            "chain": chain,
            "facilitator": _item_facilitator(item),
            "method": probe.extract_method(item),
            "inputSchema_present": bool(probe.extract_input_schema(item)),
        }
        if tid in PREFERRED_SAMPLE_THEMES:
            preferred_by_theme[tid].append(sample)
        elif tid in DEFER_SAMPLE_THEMES or _deferred_need(need):
            deferred.append(sample)
        else:
            extra.append(sample)

    picked: list[dict] = []
    seen: set[str] = set()

    def take_one(sample: dict) -> bool:
        if len(picked) >= MAX_SAMPLES:
            return False
        key = sample["need"].lower()
        if key in seen:
            return False
        seen.add(key)
        picked.append(sample)
        return True

    def take(bucket: list[dict]) -> None:
        for sample in bucket:
            if len(picked) >= MAX_SAMPLES:
                return
            take_one(sample)

    def hinted(tid: str, bucket: list[dict]) -> list[dict]:
        hints = _THEME_HINTS.get(tid) or ()
        if not hints:
            return bucket
        hits = []
        rest = []
        for sample in bucket:
            blob = f"{sample.get('need') or ''} {sample.get('url') or ''}".lower()
            if any(h in blob for h in hints):
                hits.append(sample)
            else:
                rest.append(sample)
        return hits + rest

    for tid in PREFERRED_SAMPLE_THEMES:
        bucket = hinted(tid, preferred_by_theme.get(tid) or [])
        if bucket:
            take_one(bucket[0])
        if len(picked) >= MAX_SAMPLES:
            break
    if len(picked) < MAX_SAMPLES:
        for tid in PREFERRED_SAMPLE_THEMES:
            take(hinted(tid, preferred_by_theme.get(tid) or []))
            if len(picked) >= MAX_SAMPLES:
                break
    if len(picked) < MAX_SAMPLES:
        take(extra)
    if len(picked) < 2:
        take(deferred)
    return picked


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _insight(chain: str, count: int, themes: list[dict]) -> str:
    label = CHAIN_LABELS.get(chain, chain)
    if count < 5:
        return f"Too few {label} listings to call a trend."
    other_share = 0.0
    for row in themes:
        if row.get("id") == "other":
            other_share = float(row.get("share") or 0)
            break
    if other_share > 0.4:
        return f"{label} still has a large unlabeled share; listings lack names"
    top = None
    for row in themes:
        if row.get("id") != "other" and int(row.get("count") or 0) > 0:
            top = row
            break
    if not top:
        return f"{label} listings are mostly uncategorized."
    share = float(top.get("share") or 0)
    if share >= 0.4:
        return f"{label} listings skew {top['label']}."
    return f"{label} is mixed; {top['label']} leads."


def _themes_for_items(items: list[dict]) -> tuple[int, list[dict]]:
    buckets: dict[str, dict] = {}
    kept = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        url = probe._resource_url(item)
        if not url or _is_ours(url):
            continue
        kept += 1
        tid = theme_id_for(item, url)
        b = buckets.setdefault(tid, {"prices": [], "examples": [], "count": 0, "unlabeled": 0})
        b["count"] += 1
        usd = _item_price_usd(item)
        if usd is not None:
            b["prices"].append(usd)
        cap = 5 if tid == "other" else 3
        if len(b["examples"]) < cap:
            href = _https_href(url)
            if href:
                b["examples"].append(href)
            elif fixtures.fixture_mode() and str(url).startswith("https://"):
                b["examples"].append(url)
        if tid == "other":
            name = _listing_name(item, url)
            host = (urlparse(url).hostname or "")[:80]
            desc = str(item.get("description") or "").strip()
            if name == host and not desc:
                b["unlabeled"] += 1
    total = kept
    themes: list[dict] = []
    for tid in THEME_ORDER:
        b = buckets.get(tid)
        if not b or b["count"] <= 0:
            continue
        share = (b["count"] / total) if total else 0.0
        row = {
            "id": tid,
            "label": THEME_LABELS[tid],
            "count": b["count"],
            "share": round(share, 4),
            "median_price": _median(b["prices"]),
        }
        if b["examples"]:
            row["examples"] = b["examples"][:5] if tid == "other" else b["examples"][:3]
        if tid == "other" and b["unlabeled"]:
            row["unlabeled"] = b["unlabeled"]
        themes.append(row)
    themes.sort(key=lambda r: (-int(r["count"]), THEME_ORDER.index(r["id"])))
    return total, themes


def normalize_item(item: dict, fallback_chain: str) -> list[dict]:
    """Kept for price tests; dashboard no longer dumps every listing."""
    url = probe._resource_url(item)
    if not url:
        return []
    accepts = _accepts(item)
    rails: list[str] = []
    prices: dict[str, tuple[str, float | None]] = {}
    for acc in accepts:
        rail = payment.rail_of_network(acc.get("network") or "")
        if not rail:
            rail = probe._item_rail(item) if not rails else None
        if rail not in CHAINS:
            continue
        if rail not in rails:
            rails.append(rail)
            prices[rail] = _price_from_accept(acc)
    if not rails:
        rail = fallback_chain if fallback_chain in CHAINS else probe._item_rail(item)
        if rail not in CHAINS:
            rail = "base"
        rails = [rail]
        prices[rail] = ("unknown", None)
        if accepts:
            prices[rail] = _price_from_accept(accepts[0])
    name = _listing_name(item, url)
    out = []
    for rail in rails:
        price, price_usd = prices.get(rail, ("unknown", None))
        out.append(
            {
                "chain": rail,
                "name": name,
                "url": url,
                "price": price,
                "price_usd": price_usd,
                "ours": _is_ours(url),
            }
        )
    return out


def _stale_chain(chain: str, error: str) -> dict:
    prev = _last_good.get(chain)
    age = None
    ts = _last_good_at.get(chain)
    if ts:
        age = max(0, int(time.time() - ts))
    if prev:
        out = dict(prev)
        out["source"] = {
            "ok": False,
            "stale": True,
            "error": error,
            "age_s": age,
        }
        return out
    return {
        "count": 0,
        "source": {"ok": False, "stale": False, "error": error, "age_s": age},
        "themes": [],
        "insight": f"Too few {CHAIN_LABELS.get(chain, chain)} listings to call a trend.",
        "samples": [],
    }


def _remember(chain: str, payload: dict) -> None:
    _last_good[chain] = {
        "count": payload.get("count") or 0,
        "source": dict(payload.get("source") or {}),
        "themes": list(payload.get("themes") or []),
        "insight": payload.get("insight") or "",
        "samples": list(payload.get("samples") or []),
    }
    _last_good_at[chain] = time.time()


def _fetch_catalog(rail: str, url: str):
    """Fail-closed: refuse anything not on the hardcoded HTTPS allowlist.

    Returns [] for allowlist miss or an empty catalog. Returns None on fetch error
    so the dashboard can keep a stale snapshot instead of freezing blank.
    """
    if not probe.catalog_url_allowed(url):
        return []
    try:
        timeout = max(probe.probe_timeout(), 8.0)
        return probe._fetch_one_catalog(rail, url, timeout)
    except Exception:
        return None


def _chain_payload(chain: str, items: list[dict], source: dict) -> dict:
    count, themes = _themes_for_items(items)
    payload = {
        "count": count,
        "source": source,
        "themes": themes,
        "insight": _insight(chain, count, themes),
        "samples": _samples_for_items(chain, items),
    }
    return payload


def _upstream_chain(chain: str, url: str) -> dict:
    """Honest unknown totals. Do not invent a local 14k from a missing mirror."""
    host = (urlparse(url).hostname or "").lower()
    label = CHAIN_LABELS.get(chain, chain)
    return {
        "count": None,
        "source": {
            "ok": True,
            "host": host or "upstream",
            "catalog": "upstream",
        },
        "themes": [],
        "insight": (
            f"{label} catalog is queried at request time; "
            "402signal does not store a local index."
        ),
        "samples": [],
    }


def _upstream_chains() -> dict[str, dict]:
    chains: dict[str, dict] = {}
    for rail, url in probe.pulse_catalogs():
        if rail not in CHAINS:
            continue
        if not probe.catalog_url_allowed(url):
            chains[rail] = _stale_chain(rail, "not_allowlisted")
            continue
        chains[rail] = _upstream_chain(rail, url)
    for chain in CHAINS:
        if chain not in chains:
            chains[chain] = _stale_chain(chain, "missing")
    return chains


def _observed_for_pulse() -> dict:
    observed = {"n_7d": 0, "reliability": "unknown", "source": "402signal_observed"}
    try:
        from live402 import history as history_mod
        observed = history_mod.pulse_observed()
    except Exception:
        observed = {"n_7d": 0, "reliability": "unknown", "source": "402signal_observed"}
    if not isinstance(observed, dict):
        observed = {"n_7d": 0, "reliability": "unknown", "source": "402signal_observed"}
    n_7d = 0
    try:
        n_7d = int(observed.get("n_7d") or 0)
    except (TypeError, ValueError):
        n_7d = 0
    if n_7d < 10:
        observed.pop("healthy", None)
        observed.pop("success_7d", None)
        observed.pop("executable_now_rate", None)
        observed.pop("payable_rate_7d", None)
        observed.pop("invocable_rate_7d", None)
        observed["reliability"] = "unknown"
    observed.pop("healthy", None)
    observed.pop("executable_now_rate", None)
    return observed


def _upstream_payload() -> dict:
    """Observed-only pulse. No local catalog totals. Never waits on discovery."""
    chains = _upstream_chains()
    return {
        "ok": True,
        "updated_at": probe.now_iso(),
        "cached_s": CACHE_TTL,
        "index_status": "upstream",
        "chains": chains,
        "samples": _mixed_samples(chains),
        "observed": _observed_for_pulse(),
    }


def _collect() -> dict:
    chains: dict[str, dict] = {}
    if fixtures.fixture_mode():
        by_chain: dict[str, list[dict]] = {c: [] for c in CHAINS}
        for item in fixtures.load_resources():
            if not isinstance(item, dict):
                continue
            for rail in _item_chains(item, "base"):
                by_chain[rail].append(item)
        for chain in CHAINS:
            payload = _chain_payload(
                chain,
                by_chain[chain],
                {"ok": True, "host": "fixture"},
            )
            chains[chain] = payload
            _remember(chain, payload)
        samples = _mixed_samples(chains)
        return {
            "ok": True,
            "updated_at": probe.now_iso(),
            "cached_s": CACHE_TTL,
            "index_status": "fixture",
            "chains": chains,
            "samples": samples,
            "observed": _observed_for_pulse(),
        }
    return _upstream_payload()


def get_pulse() -> dict:
    """In-memory cache ~15s. Query strings are never read here.

    Single-flight: one in-flight _collect. Waiters return last-good immediately
    (or a cheap upstream payload). Never calls get_index/refresh/query_for_need.
    Never invents local catalog totals.
    """
    global _collecting
    now = time.monotonic()
    with _lock:
        payload = _cache.get("payload")
        if payload is not None and (now - _cache["at"]) < CACHE_TTL:
            return payload

    cheap = False
    with _lock:
        payload = _cache.get("payload")
        now = time.monotonic()
        if payload is not None and (now - _cache["at"]) < CACHE_TTL:
            return payload
        if payload is not None and _collecting:
            return payload
        if _collecting:
            cheap = True
        else:
            _collecting = True

    if cheap:
        return _upstream_payload()

    try:
        built = _collect()
    except Exception:
        with _lock:
            _collecting = False
        raise
    with _lock:
        _cache["at"] = time.monotonic()
        _cache["payload"] = built
        _collecting = False
    return built


def _working_total(working: dict | None):
    """Pass through an upstream pagination.total when present. Never invent one."""
    if not isinstance(working, dict):
        return None
    pag = working.get("pagination")
    raw = None
    if isinstance(pag, dict) and pag.get("total") is not None:
        raw = pag.get("total")
    elif working.get("total") is not None:
        raw = working.get("total")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _attach_preview_observations(hits: list[dict]) -> None:
    """Read-only sqlite join. Never probes. Missing history → not_yet_observed."""
    if not hits:
        return
    try:
        from live402 import history as history_mod
        urls = [str(h.get("url") or "") for h in hits if isinstance(h, dict)]
        obs_map = history_mod.preview_observations(urls)
    except Exception:
        obs_map = {}
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        url = str(hit.get("url") or "")
        row = obs_map.get(url)
        if not isinstance(row, dict):
            row = {"status": "not_yet_observed"}
        hit["observation"] = row
        try:
            from live402 import reputation as reputation_mod

            fake = {"url": url, "history": {"n_7d": row.get("n_7d"), "success_7d": row.get("success_7d")}}
            hit["reputation"] = reputation_mod.attach(fake).get("reputation")
        except Exception:
            pass


def preview_need(need: str, prefer_network: str | None = None, networks=None) -> dict:
    """Request-time catalog search. Never probes. Never charges. Rail-neutral unless asked."""
    raw = (need or "").strip()
    freshness = probe.now_iso()
    empty = {
        "need": raw,
        "not_probed": True,
        "freshness": freshness,
        "cached_s": None,
        "discovery_matches": 0,
        "displayed": 0,
        "hits": [],
        "discovery_via": {},
        "discovery_exhaustive": False,
    }
    if not raw:
        empty["miss_reason"] = "invalid_need"
        return empty
    prefer = probe.normalize_prefer_network(prefer_network)
    named = prefer or named_chain(raw)
    rails = probe.normalize_networks(networks)
    try:
        working = catalog.query_for_need(raw, prefer_network=named, networks=rails)
    except Exception:
        working = {"items": []}
    items = list(working.get("items") or [])
    ranked = probe.rank_resources(raw, items, prefer_network=named)
    rails_up = _rails_up_map()
    by_need_chains: dict[str, set[str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        url = probe._resource_url(item)
        chain = probe._item_rail(item)
        need_key = (sample_need_for(item, url) or raw).strip().lower()
        if need_key and chain in CHAINS:
            by_need_chains.setdefault(need_key, set()).add(chain)
    candidates: list[dict] = []
    seen: set[str] = set()
    for item in ranked:
        if not isinstance(item, dict):
            continue
        url = probe._resource_url(item)
        if not url or probe.skip_candidate_url(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        chain = probe._item_rail(item)
        if chain not in CHAINS:
            chain = ""
        href = _https_href(url) or (
            url if fixtures.fixture_mode() and str(url).startswith("https://") else ""
        )
        if not href:
            continue
        label = sample_need_for(item, url) or raw
        fac = _item_facilitator(item)
        row = {
            "need": label,
            "label": label,
            "url": href,
            "price": _item_price_label(item),
            "chain": chain or None,
            "facilitator": fac,
            "method": probe.extract_method(item),
            "inputSchema_present": bool(
                item.get("_input_schema_present") or probe.extract_input_schema(item)
            ),
            "rails_up": rails_up.get(chain) if chain else None,
        }
        need_key = str(row.get("need") or "").strip().lower()
        others = sorted((by_need_chains.get(need_key) or set()) - ({chain} if chain else set()))
        if others:
            row["also_on"] = others
        candidates.append(row)
    discovery_matches = len(candidates)
    hits = candidates[:PREVIEW_DISPLAY]
    _attach_preview_observations(hits)
    body = {
        "need": raw,
        "not_probed": True,
        "freshness": freshness,
        "cached_s": None,
        "discovery_matches": discovery_matches,
        "displayed": len(hits),
        "hits": hits,
        "discovery_via": catalog.public_discovery_via(working),
        "discovery_exhaustive": catalog.discovery_exhaustive(working),
    }
    if discovery_matches > len(hits):
        body["truncated"] = True
    world_total = _working_total(working)
    if world_total is not None:
        body["total"] = world_total
    return body


def _esc(value: str) -> str:
    return html.escape(value or "", quote=True)


def _https_href(url: str) -> str | None:
    raw = (url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    return raw


def _source_line(source: dict) -> str:
    if not source:
        return "source unknown"
    if source.get("ok"):
        host = source.get("host") or "catalog"
        return f"source ok · {_esc(str(host))}"
    if source.get("stale"):
        age = source.get("age_s")
        age_s = f" · stale {int(age)}s" if age is not None else " · stale"
        return f"source fail{age_s}"
    return "source fail"


def _home_href(sample: dict) -> str:
    q = {"need": sample.get("need") or ""}
    href = _https_href(str(sample.get("url") or "")) or ""
    if href:
        q["url"] = href
    elif fixtures.fixture_mode() and str(sample.get("url") or "").startswith("https://"):
        q["url"] = str(sample.get("url"))
    return "/?" + urlencode(q)


def _column_inner(chain: str, data: dict) -> str:
    source = data.get("source") or {}
    stale_cls = " stale" if source.get("stale") or not source.get("ok") else ""
    if source.get("ok"):
        stale_cls = ""
    samples = data.get("samples") or []
    rows = []
    for s in samples:
        if not isinstance(s, dict):
            continue
        need = _esc(str(s.get("label") or s.get("need") or ""))
        price = _esc(str(s.get("price") or ""))
        raw_url = str(s.get("url") or "")
        href = _https_href(raw_url) or (raw_url if fixtures.fixture_mode() and raw_url.startswith("https://") else "")
        host = _esc((urlparse(href).hostname or "")[:80]) if href else ""
        home = _esc(_home_href(s))
        rows.append(
            f'<a class="lookup" href="{home}">'
            f'<div class="lookup-row"><span>{need}</span><span class="muted">{price}</span></div>'
            f'<div class="lookup-host">{host}</div>'
            f"</a>"
        )
    if not rows:
        rows.append('<p class="muted">No sample lookups this snapshot.</p>')
    return (
        f'<h2>{CHAIN_LABELS.get(chain, chain)}</h2>'
        f'<p class="age{stale_cls}">{_source_line(source)}</p>'
        f'<div class="lookups">{"".join(rows)}</div>'
    )


def dashboard_html(payload: dict | None = None) -> str:
    data = payload or get_pulse()
    updated = _esc(str(data.get("updated_at") or ""))
    chains = data.get("chains") or {}
    cols = []
    for chain in CHAINS:
        inner = _column_inner(chain, chains.get(chain) or {})
        cols.append(f'<section class="col" id="chain-{chain}" data-chain="{chain}">{inner}</section>')
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Examples</title>
  <link rel="stylesheet" href="/styles.css" />
</head>
<body>
  <div class="page wide">
    <header class="mast">
      <div class="brand-row">
        <div class="mark">402</div>
        <h1>Lookups we can try</h1>
      </div>
      <p class="sub">These came from public Base / Solana / Algorand catalogs. We still probe before you pay.</p>
      <nav class="nav"><a href="/">Home</a> · <a href="/dashboard">Examples</a> · <a href="/pulse">JSON</a></nav>
    </header>
    <p class="muted">Last updated <time id="updated-at">{updated}</time> · refreshes about every 20s</p>
    <div class="board" id="board">
      {"".join(cols)}
    </div>
    <p class="ours-note">402Signal itself: <a href="https://402signal.com/route" rel="noopener noreferrer">402signal.com/route</a>. Pay $0.01 USDC for a live payable URL or an honest miss.</p>
    <footer class="foot">
      <p><a href="https://402signal.com">402signal.com</a> · <a href="https://x.com/402Signal" rel="noopener noreferrer">@402Signal</a></p>
    </footer>
  </div>
  <script src="/dashboard.js"></script>
</body>
</html>
"""
