"""Request-time discovery query. Slim records, capability labels.

Never copies the three x402 catalogs into process memory. No daemon TTL crawl.
CDP is queried via /discovery/search. PayAI and GoPlausible use search when
the host serves it, else a small first-pages fetch. Pagination is
limit+offset+total only. Never send page= or cursor=. Never fetch caller URLs.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from live402 import fixtures, payment, probe

PAGE_SIZE = 100
# Need-scoped working set only. Do not walk PayAI's ~279 pages or accumulate 30k.
QUERY_MAX_PAGES = 2
QUERY_MAX_ITEMS = 100
SEARCH_LIMIT = 20
NEED_QUERY_MAX = 200
# Raw CDP pages include huge schemas; 1MiB/page then slim immediately. Oversize pages dropped.
PAGE_READ_LIMIT = 1_048_576
CDP_SEARCH = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search"
PAYAI_SEARCH = "https://facilitator.payai.network/discovery/search"
GOPL_SEARCH = "https://facilitator.goplausible.xyz/discovery/search"
SEARCH_BASES = {
    "base": CDP_SEARCH,
    "solana": PAYAI_SEARCH,
    "algorand": GOPL_SEARCH,
}
# CDP search accepts CAIP-2 or legacy names. Only pass our hardcoded rail map.
_RAIL_NETWORK = {
    "base": "eip155:8453",
    "solana": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
    "algorand": "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=",
}

_RAILS = ("base", "solana", "algorand")
_DROP_QUERY = frozenset(
    {"limit", "offset", "page", "cursor", "query", "network", "urlsubstring"}
)
_GENERIC_URL = frozenset(
    {
        "api",
        "v0",
        "v1",
        "v2",
        "v3",
        "v4",
        "http",
        "https",
        "www",
        "com",
        "index",
        "json",
        "xml",
        "html",
        "x402",
        "mcp",
        "rest",
        "public",
        "chain",
        "chains",
        "base",
        "solana",
        "algorand",
        "ethereum",
        "mainnet",
        "testnet",
        "network",
        "resources",
        "discovery",
        "platform",
        "data",
        "service",
        "services",
        "endpoint",
        "endpoints",
    }
)
# URL-only classification: distinctive tokens only. Generic paths stay unknown.
_URL_STRONG = frozenset(
    {
        "weather",
        "forecast",
        "climate",
        "meteo",
        "nft",
        "nfts",
        "opensea",
        "erc721",
        "erc1155",
        "websearch",
        "serp",
        "inference",
        "honeypot",
        "kyc",
        "siwe",
        "oauth",
        "ipfs",
        "ohlc",
        "ohlcv",
        "ticker",
        "erc20",
        "allowance",
        "coinflip",
    }
)

# First unique match wins per evidence source. Do not classify from rail names.
_CAPABILITY_RULES: tuple[tuple[str, frozenset[str]], ...] = (
    ("travel.weather", frozenset({"weather", "forecast", "climate", "temperature", "meteo"})),
    ("nft.collectible", frozenset({"nft", "nfts", "collectible", "opensea", "erc721", "erc1155"})),
    (
        "identity.auth",
        frozenset({"identity", "auth", "kyc", "login", "did", "siwe", "oauth", "signin"}),
    ),
    (
        "games.play",
        frozenset({"game", "games", "chess", "coinflip", "casino", "bet", "wager"}),
    ),
    (
        "storage.files",
        frozenset({"upload", "file", "files", "blob", "ipfs", "store", "bucket"}),
    ),
    (
        "search.web",
        frozenset({"search", "google", "websearch", "browse", "serp", "lookup"}),
    ),
    (
        "compute.inference",
        frozenset(
            {
                "llm",
                "grok",
                "gpt",
                "compute",
                "inference",
                "openai",
                "claude",
                "generate",
            }
        ),
    ),
    (
        "messaging.notify",
        frozenset({"message", "email", "sms", "notify", "slack", "telegram", "webhook", "inbox"}),
    ),
    (
        "payments.checkout",
        frozenset({"invoice", "checkout", "payout", "billing", "merchant", "payroll", "remittance"}),
    ),
    (
        "market.price",
        frozenset({"price", "market", "ticker", "quote", "trading", "swap", "dex", "candle", "ohlc", "ohlcv", "tvl"}),
    ),
    (
        "security.token_risk",
        frozenset({"honeypot", "rugpull", "tokenrisk", "scam", "phishing", "malicious"}),
    ),
    (
        "chain.balance",
        frozenset({"balance", "erc20", "allowance", "onchain", "tokenbalance"}),
    ),
)

def _empty_index() -> dict:
    return {
        "items": [],
        "by_rail": {rail: [] for rail in _RAILS},
        "fetched_at": 0.0,
        "totals": {},
        "truncated": {},
        "complete": False,
        "errors": {},
    }


def reset_index() -> None:
    """No-op. There is no in-RAM world index to drop. Tests still call this."""
    return


def refresh_in_progress() -> bool:
    """Always False. There is no daemon catalog walk."""
    return False


def page_url(base: str, limit: int, offset: int) -> str | None:
    """Build an allowlisted catalog URL with only limit+offset. Never page/cursor."""
    raw = (base or "").strip()
    if not probe.catalog_url_allowed(raw):
        return None
    try:
        lim = int(limit)
        off = int(offset)
    except (TypeError, ValueError):
        return None
    if lim < 1 or off < 0:
        return None
    parsed = urlparse(raw)
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _DROP_QUERY
    ]
    query.append(("limit", str(lim)))
    query.append(("offset", str(off)))
    built = urlunparse(parsed._replace(query=urlencode(query)))
    if not probe.catalog_url_allowed(built):
        return None
    return built


def _clip_need_query(need: str) -> str:
    text = " ".join((need or "").split())
    if not text:
        return ""
    return text[:NEED_QUERY_MAX]


def search_url(
    base: str,
    query: str,
    limit: int,
    offset: int = 0,
    network: str | None = None,
    url_substring: str | None = None,
) -> str | None:
    """Allowlisted search URL. query + limit + offset only. Never page/cursor.

    network and url_substring are optional hardcoded filters (CDP). The query
    string is never used as a fetch target.
    """
    raw = (base or "").strip()
    if not probe.catalog_url_allowed(raw):
        return None
    q = _clip_need_query(query)
    if not q and not url_substring:
        return None
    try:
        lim = int(limit)
        off = int(offset)
    except (TypeError, ValueError):
        return None
    if lim < 1 or off < 0:
        return None
    parsed = urlparse(raw)
    params = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _DROP_QUERY
    ]
    if q:
        params.append(("query", q))
    if network:
        net = str(network).strip()
        if net not in _RAIL_NETWORK.values() and net not in _RAIL_NETWORK:
            return None
        params.append(("network", _RAIL_NETWORK.get(net, net)))
    if url_substring:
        sub = str(url_substring).strip()[:2048]
        if len(sub) < 3:
            return None
        params.append(("urlSubstring", sub))
    params.append(("limit", str(lim)))
    params.append(("offset", str(off)))
    built = urlunparse(parsed._replace(query=urlencode(params)))
    if not probe.catalog_url_allowed(built):
        return None
    return built


def _items_from_payload(payload) -> list[dict]:
    if isinstance(payload, list):
        raw = payload
    elif isinstance(payload, dict):
        raw = payload.get("items") or payload.get("resources") or []
    else:
        raw = []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _nonneg_int(val):
    if isinstance(val, bool):
        return None
    if isinstance(val, int) and val >= 0:
        return val
    if isinstance(val, float) and val >= 0:
        return int(val)
    if isinstance(val, str) and val.strip().isdigit():
        return int(val.strip())
    return None


def parse_pagination(payload, requested_limit: int = PAGE_SIZE) -> dict:
    """Read payload['pagination']. Never invent a total."""
    items = _items_from_payload(payload)
    n = len(items)
    try:
        req = int(requested_limit)
    except (TypeError, ValueError):
        req = PAGE_SIZE
    out = {
        "limit": None,
        "offset": None,
        "total": None,
        "has_pagination": False,
        "last": False,
    }
    pag = payload.get("pagination") if isinstance(payload, dict) else None
    if isinstance(pag, dict):
        out["has_pagination"] = True
        for key in ("limit", "offset", "total"):
            parsed = _nonneg_int(pag.get(key))
            if parsed is not None:
                out[key] = parsed
        if n == 0:
            out["last"] = True
        elif out["total"] is not None:
            base = out["offset"] if out["offset"] is not None else 0
            out["last"] = (base + n) >= out["total"]
        elif out["limit"] is not None and n < out["limit"]:
            out["last"] = True
        return out
    # Missing pagination: short page is last; full page → caller may try next once.
    # Never invent total.
    if n < req:
        out["last"] = True
    return out


def _clip(val, n: int) -> str | None:
    if val is None:
        return None
    text = str(val).strip()
    if not text:
        return None
    return text[:n]


def _tool_name(item: dict) -> str:
    bazaar = ((item.get("extensions") or {}).get("bazaar") or {})
    info = bazaar.get("info") or {} if isinstance(bazaar, dict) else {}
    inp = info.get("input") or {} if isinstance(info, dict) else {}
    if isinstance(inp, dict) and inp.get("toolName"):
        return str(inp.get("toolName") or "")
    if item.get("toolName"):
        return str(item.get("toolName") or "")
    return ""


def _match_capabilities(toks: set[str]) -> list[str]:
    hits: list[str] = []
    for cap, keywords in _CAPABILITY_RULES:
        if toks & keywords:
            hits.append(cap)
    return hits


def _url_tokens(url: str) -> set[str]:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    labels = [p for p in host.split(".") if p]
    host_blob = " ".join(labels[:-1] if len(labels) > 1 else labels)
    toks = probe._tokens(f"{host_blob} {path}")
    return {t for t in toks if t not in _GENERIC_URL and t not in probe.STOP}


def classify_capability(item: dict | None) -> tuple[str, str]:
    """Rule-based capability. unknown if low confidence. No LLM. No chain-rail names."""
    if not isinstance(item, dict):
        return "unknown", "unknown"
    tags = item.get("tags") or []
    if isinstance(tags, list):
        tag_text = " ".join(str(t) for t in tags)
    else:
        tag_text = str(tags)
    sources: list[tuple[str, set[str]]] = [
        ("tags", probe._tokens(tag_text)),
        ("toolName", probe._tokens(_tool_name(item))),
        ("description", probe._tokens(str(item.get("description") or ""))),
        ("serviceName", probe._tokens(str(item.get("serviceName") or ""))),
    ]
    for source, toks in sources:
        hits = _match_capabilities(toks)
        if len(hits) == 1:
            return hits[0], source
    url = probe._resource_url(item)
    url_toks = _url_tokens(url)
    hits = _match_capabilities(url_toks)
    if len(hits) == 1 and (url_toks & _URL_STRONG):
        return hits[0], "url"
    return "unknown", "unknown"


def capability_for_need(need: str) -> str:
    cap, _src = classify_capability({"description": need or ""})
    return cap


def _slim_extra(extra: dict) -> dict:
    out: dict = {}
    if "facilitator" in extra:
        raw = extra.get("facilitator")
        if isinstance(raw, str) and raw.strip().startswith("https://"):
            out["facilitator"] = raw.strip()
        elif isinstance(raw, dict):
            fac: dict = {}
            url = str(raw.get("url") or "").strip()
            if url.startswith("https://"):
                fac["url"] = url
            if raw.get("feePayer"):
                fac["feePayer"] = raw.get("feePayer")
            if fac:
                out["facilitator"] = fac
    if extra.get("feePayer"):
        out["feePayer"] = extra.get("feePayer")
    display = extra.get("displayAmount")
    if display is not None and str(display).strip():
        out["displayAmount"] = str(display).strip()
    return out


def _slim_accepts(item: dict) -> list[dict]:
    out: list[dict] = []
    raw = item.get("accepts") or []
    if not isinstance(raw, list):
        return out
    for acc in raw:
        if not isinstance(acc, dict):
            continue
        row: dict = {}
        for key in ("payTo", "network", "scheme", "asset", "currency"):
            if acc.get(key) is not None:
                row[key] = acc[key]
        amount = acc.get("amount")
        if amount is None:
            amount = acc.get("maxAmountRequired")
        if amount is not None:
            row["amount"] = amount
        extra = acc.get("extra") if isinstance(acc.get("extra"), dict) else {}
        slim_extra = _slim_extra(extra)
        if slim_extra:
            row["extra"] = slim_extra
        if row:
            out.append(row)
    return out


def _slim_quality(item: dict) -> dict | None:
    quality = item.get("quality")
    if not isinstance(quality, dict):
        return None
    out: dict = {}
    for key in ("l30DaysTotalCalls", "l30DaysUniquePayers"):
        if key in quality:
            parsed = _nonneg_int(quality.get(key))
            if parsed is not None:
                out[key] = parsed
            elif quality.get(key) is not None and not isinstance(quality.get(key), (dict, list)):
                out[key] = quality.get(key)
    return out or None


def _slim_bazaar(item: dict) -> dict | None:
    bazaar = ((item.get("extensions") or {}).get("bazaar") or {})
    if not isinstance(bazaar, dict):
        return None
    info = bazaar.get("info") or {}
    if not isinstance(info, dict):
        return None
    inp = info.get("input") or {}
    if not isinstance(inp, dict):
        return None
    slim_inp: dict = {}
    for key in ("method", "toolName", "type"):
        if inp.get(key) is not None:
            slim_inp[key] = inp.get(key)
    if not slim_inp:
        return None
    return {"info": {"input": slim_inp}}


def _copy_url_fields(item: dict, slim: dict) -> None:
    for key in ("resource", "resourceUrl", "url"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            slim[key] = val.strip()
        elif isinstance(val, dict):
            inner = val.get("url") or val.get("resourceUrl") or val.get("resource") or ""
            if inner:
                slim[key] = str(inner).strip()


def _slim_metadata(meta) -> dict | None:
    if not isinstance(meta, dict):
        return None
    keys = (
        "x402Requests",
        "requestCount",
        "totalRequests",
        "requests",
        "qualityCalls",
        "calls",
        "settleCount",
    )
    out: dict = {}
    for key in keys:
        parsed = _nonneg_int(meta.get(key))
        if parsed is not None:
            out[key] = parsed
    disc = meta.get("discovery")
    if isinstance(disc, dict):
        nested: dict = {}
        for key in keys:
            parsed = _nonneg_int(disc.get(key))
            if parsed is not None:
                nested[key] = parsed
        if nested:
            out["discovery"] = nested
    return out or None


def _slim_discovery_info(info) -> dict | None:
    if not isinstance(info, dict):
        return None
    out: dict = {}
    for key, val in info.items():
        low = str(key).lower()
        if "schema" in low:
            continue
        if isinstance(val, str):
            clipped = _clip(val, 200)
            if clipped:
                out[key] = clipped
        else:
            parsed = _nonneg_int(val)
            if parsed is not None:
                out[key] = parsed
    return out or None


def slim_item(item: dict | None, rail: str) -> dict:
    """Keep ranking/pulse fields. Drop huge schema blobs. Classify at ingest."""
    if not isinstance(item, dict):
        item = {}
    in_schema = probe.extract_input_schema(item)
    out_schema = probe.extract_output_schema(item)
    cap, cap_src = classify_capability(item)

    slim: dict = {}
    _copy_url_fields(item, slim)
    desc = _clip(item.get("description"), 500)
    if desc:
        slim["description"] = desc
    name = _clip(item.get("serviceName"), 120)
    if name:
        slim["serviceName"] = name
    if item.get("type") is not None:
        slim["type"] = item.get("type")
    tags = item.get("tags")
    if isinstance(tags, list):
        slim["tags"] = [str(t)[:80] for t in tags[:16]]
    accepts = _slim_accepts(item)
    if accepts:
        slim["accepts"] = accepts
    quality = _slim_quality(item)
    if quality:
        slim["quality"] = quality
    parsed_settle = _nonneg_int(item.get("settleCount"))
    if parsed_settle is not None:
        slim["settleCount"] = parsed_settle
    for key in (
        "x402Requests",
        "requestCount",
        "totalRequests",
        "requests",
        "qualityCalls",
        "calls",
    ):
        parsed = _nonneg_int(item.get(key))
        if parsed is not None:
            slim[key] = parsed
    bazaar = _slim_bazaar(item)
    if bazaar:
        slim["extensions"] = {"bazaar": bazaar}
    meta = _slim_metadata(item.get("metadata"))
    if meta:
        slim["metadata"] = meta
    dinfo = _slim_discovery_info(item.get("discoveryInfo"))
    if dinfo:
        slim["discoveryInfo"] = dinfo
    updated = _clip(item.get("lastUpdated"), 80)
    if updated:
        slim["lastUpdated"] = updated
    slim["_input_schema_present"] = bool(in_schema)
    slim["_output_schema_present"] = bool(out_schema)
    slim["_rail"] = rail
    slim["capability"] = cap
    slim["capability_source"] = cap_src
    return slim


def _step_offset(pag: dict, n_items: int) -> int:
    """Advance by returned pagination.limit (CDP clamp) or len(items). Never by guessed page=."""
    step = pag.get("limit")
    if isinstance(step, int) and step > 0:
        return step
    if n_items > 0:
        return n_items
    return 0


def fetch_rail(
    rail: str,
    base_url: str,
    max_pages: int = QUERY_MAX_PAGES,
    max_items: int = QUERY_MAX_ITEMS,
) -> dict:
    """Walk a few first pages only. Never MAX_ITEMS across the world catalog."""
    items: list[dict] = []
    seen: set[str] = set()
    offset = 0
    pages = 0
    truncated = False
    total = None
    error = None
    tried_extra = False
    timeout = max(probe.probe_timeout(), 8.0)
    try:
        page_cap = int(max_pages)
        item_cap = int(max_items)
    except (TypeError, ValueError):
        page_cap = QUERY_MAX_PAGES
        item_cap = QUERY_MAX_ITEMS
    if page_cap < 1:
        page_cap = 1
    if item_cap < 1:
        item_cap = 1

    while pages < page_cap and len(items) < item_cap:
        url = page_url(base_url, PAGE_SIZE, offset)
        if not url:
            if pages == 0:
                error = "not_allowlisted"
            break
        try:
            payload = probe._fetch_catalog_payload(url, timeout, read_limit=PAGE_READ_LIMIT)
        except Exception:
            error = "fetch_failed"
            break
        page_items = _items_from_payload(payload)
        pag = parse_pagination(payload, requested_limit=PAGE_SIZE)
        if pag.get("total") is not None:
            total = pag["total"]

        n = len(page_items)
        for item in page_items:
            slim = slim_item(item, rail)
            key = probe._resource_url(slim)
            if not key:
                continue
            if key in seen:
                continue
            seen.add(key)
            items.append(slim)
            if len(items) >= item_cap:
                truncated = True
                break

        pages += 1
        if truncated:
            break
        if n == 0:
            break

        step = _step_offset(pag, n)
        if step <= 0:
            break
        offset += step

        if total is not None and offset >= total:
            break
        if pag.get("last"):
            break
        if not pag.get("has_pagination"):
            if n < PAGE_SIZE:
                break
            if tried_extra:
                break
            tried_extra = True

    if pages >= page_cap or len(items) >= item_cap:
        truncated = True

    return {
        "items": items,
        "total": total,
        "truncated": truncated,
        "complete": (not truncated) and error is None,
        "error": error,
        "pages": pages,
        "count": len(items),
    }


def _merge_accepts(dest: dict, src: dict) -> None:
    """Keep every rail's payment terms when the same URL is listed twice."""
    existing = [a for a in (dest.get("accepts") or []) if isinstance(a, dict)]
    seen = {payment.accept_identity(a) for a in existing}
    for acc in src.get("accepts") or []:
        if not isinstance(acc, dict):
            continue
        ident = payment.accept_identity(acc)
        if ident in seen:
            continue
        existing.append(acc)
        seen.add(ident)
    if existing:
        dest["accepts"] = existing


def _merge_items(by_rail: dict) -> list[dict]:
    """Dedup by resource URL across rails. Keep rails/also_on. Do not drop a rail copy.

    Reuses the by_rail item dicts (no per-item copy) so items and by_rail share
    identity. Cross-rail hits mutate rails/also_on on the first-seen dict and
    append the later rail's accepts so payment options survive URL dedupe.
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for rail in _RAILS:
        for item in by_rail.get(rail) or []:
            if not isinstance(item, dict):
                continue
            key = probe._resource_url(item)
            if not key:
                continue
            if key in merged:
                prev = merged[key]
                rails = list(prev.get("rails") or [prev.get("_rail")])
                if rail not in rails:
                    rails.append(rail)
                prev["rails"] = rails
                primary = prev.get("_rail")
                also = [r for r in rails if r != primary]
                if also:
                    prev["also_on"] = also
                _merge_accepts(prev, item)
            else:
                item["rails"] = [rail]
                item.pop("also_on", None)
                merged[key] = item
                order.append(key)
    return [merged[k] for k in order]


def _looks_like_search_payload(payload) -> bool:
    if isinstance(payload, list):
        return True
    if not isinstance(payload, dict) or not payload:
        return False
    if "items" in payload or "resources" in payload:
        return True
    if "searchMethod" in payload or "partialResults" in payload:
        return True
    if isinstance(payload.get("pagination"), dict):
        return True
    return False


def _slim_payload_items(payload, rail: str) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for item in _items_from_payload(payload):
        slim = slim_item(item, rail)
        key = probe._resource_url(slim)
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(slim)
        if len(items) >= QUERY_MAX_ITEMS:
            break
    return items


def _search_rail(rail: str, need: str, url_substring: str | None = None) -> dict:
    """One search request. Does not walk resources pages."""
    base = SEARCH_BASES.get(rail) or ""
    network = _RAIL_NETWORK.get(rail) if rail == "base" else None
    url = search_url(
        base,
        need,
        SEARCH_LIMIT,
        0,
        network=network,
        url_substring=url_substring,
    )
    if not url:
        return {"items": [], "error": "not_allowlisted", "via": "search"}
    timeout = max(probe.probe_timeout(), 8.0)
    try:
        payload = probe._fetch_catalog_payload(url, timeout, read_limit=PAGE_READ_LIMIT)
    except Exception:
        return {"items": [], "error": "fetch_failed", "via": "search"}
    if not _looks_like_search_payload(payload):
        return {"items": [], "error": "no_search", "via": "search"}
    return {
        "items": _slim_payload_items(payload, rail),
        "error": None,
        "via": "search",
    }


def _first_pages_rail(rail: str) -> dict:
    """Small first-pages fallback. Never accumulates a world copy."""
    base = ""
    for name, url in probe.CATALOGS:
        if name == rail:
            base = url
            break
    if not base:
        return {"items": [], "error": "not_allowlisted", "via": "pages"}
    result = fetch_rail(rail, base, max_pages=QUERY_MAX_PAGES, max_items=QUERY_MAX_ITEMS)
    result["via"] = "pages"
    return result


def query_rail(rail: str, need: str, url_substring: str | None = None) -> dict:
    """Search this rail, else first pages. CDP is search-only (no 14k walk)."""
    if rail not in _RAILS:
        return {"items": [], "error": "unknown_rail", "via": "search"}
    searched = _search_rail(rail, need, url_substring=url_substring)
    if rail == "base":
        return searched
    if searched.get("error") in (None,):
        return searched
    # PayAI / GoPlausible: search if they have it, else first pages.
    if searched.get("error") in ("no_search", "fetch_failed", "not_allowlisted"):
        return _first_pages_rail(rail)
    return searched


def query_for_need(need: str, prefer_network: str | None = None) -> dict:
    """Need-scoped working set. Never stores a 44k index. Never walks MAX_ITEMS.

    prefer_network scopes which rails are queried. Unscoped queries all three,
    each capped at QUERY_MAX_ITEMS — caps do not accumulate into one 30k bag.
    """
    if fixtures.fixture_mode():
        by_rail: dict[str, list] = {rail: [] for rail in _RAILS}
        for item in fixtures.load_resources():
            if not isinstance(item, dict):
                continue
            rail = probe._item_rail(item)
            if rail not in _RAILS:
                rail = "base"
            row = dict(item)
            row["_rail"] = rail
            by_rail[rail].append(row)
        prefer = probe.normalize_prefer_network(prefer_network)
        if prefer:
            by_rail = {r: (by_rail.get(r) or [] if r == prefer else []) for r in _RAILS}
        items = _merge_items(by_rail)
        return {
            "items": items,
            "by_rail": by_rail,
            "totals": {},
            "truncated": {},
            "complete": True,
            "errors": {},
            "via": {rail: "fixture" for rail in _RAILS},
        }

    q = _clip_need_query(need)
    prefer = probe.normalize_prefer_network(prefer_network)
    rails = (prefer,) if prefer else _RAILS
    by_rail: dict[str, list] = {rail: [] for rail in _RAILS}
    errors: dict = {}
    via: dict = {}
    truncated: dict = {}
    if not q:
        return {
            "items": [],
            "by_rail": by_rail,
            "totals": {},
            "truncated": truncated,
            "complete": False,
            "errors": {"need": "invalid_need"},
            "via": via,
        }

    for rail in rails:
        try:
            result = query_rail(rail, q)
        except Exception:
            result = {"items": [], "error": "fetch_failed", "via": "search"}
        got = list(result.get("items") or [])
        # Per-rail cap. Do not let leftovers from one rail raise another rail's cap.
        by_rail[rail] = got[:QUERY_MAX_ITEMS]
        via[rail] = result.get("via") or "search"
        if result.get("truncated"):
            truncated[rail] = True
        err = result.get("error")
        if err:
            errors[rail] = err

    items = _merge_items(by_rail)
    return {
        "items": items,
        "by_rail": by_rail,
        "totals": {},
        "truncated": truncated,
        "complete": not errors,
        "errors": errors,
        "via": via,
    }


def item_for_url(url: str) -> dict | None:
    """Find one listing by URL. Fixture first. Else a scoped search. Never a 44k scan."""
    raw = (url or "").strip()
    if not raw:
        return None
    found = fixtures.lookup_url(raw)
    if found:
        rail = probe._item_rail(found)
        if rail not in _RAILS:
            rail = "base"
        return slim_item(found, rail)
    if fixtures.fixture_mode():
        return None
    parsed = urlparse(raw)
    host = (parsed.hostname or "").strip()
    sub = host if len(host) >= 3 else ""
    q = raw[:NEED_QUERY_MAX]
    for rail in _RAILS:
        try:
            result = query_rail(rail, q, url_substring=sub or None)
        except Exception:
            continue
        for item in result.get("items") or []:
            if probe._resource_url(item) == raw:
                return item
    return None


def peek_index() -> dict | None:
    """Always None. We do not keep a local catalog mirror. Never refreshes."""
    return None


def get_index() -> dict:
    """Empty working set. Does not crawl. Paid /route must use query_for_need."""
    return _empty_index()


def refresh() -> dict:
    """No-op. We do not copy catalogs into RAM."""
    return _empty_index()


def start_refresher() -> None:
    """No-op. No 180s full-catalog daemon."""
    return
