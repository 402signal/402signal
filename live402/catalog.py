"""Full-catalog index: paginated discovery, slim records, capability labels.

Fly VM is 256MB — slim at ingest, never retain raw JSON schemas.
Pagination is limit+offset+total only. Never send page= or cursor=.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from live402 import probe

PAGE_SIZE = 100
INDEX_TTL = 180.0
MAX_PAGES = 400
MAX_ITEMS = 30_000
# Raw CDP pages include huge schemas; 1MiB/page then slim immediately. Oversize pages dropped.
PAGE_READ_LIMIT = 1_048_576

_RAILS = ("base", "solana", "algorand")
_DROP_QUERY = frozenset({"limit", "offset", "page", "cursor"})
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

_lock = threading.Lock()
_refresh_lock = threading.Lock()
_index: dict | None = None
_fetched_mono = 0.0
_refresher_thread: threading.Thread | None = None


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
    """Drop the in-memory catalog. Tests only."""
    global _index, _fetched_mono
    with _lock:
        _index = None
        _fetched_mono = 0.0


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
        for key in ("payTo", "network", "scheme"):
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


def fetch_rail(rail: str, base_url: str) -> dict:
    """Walk offset=0,100,200… until empty, offset>=total, MAX_PAGES, or MAX_ITEMS."""
    items: list[dict] = []
    seen: set[str] = set()
    offset = 0
    pages = 0
    truncated = False
    total = None
    error = None
    tried_extra = False
    timeout = max(probe.probe_timeout(), 8.0)

    while pages < MAX_PAGES and len(items) < MAX_ITEMS:
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
            if len(items) >= MAX_ITEMS:
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

    if pages >= MAX_PAGES or len(items) >= MAX_ITEMS:
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


def _merge_items(by_rail: dict) -> list[dict]:
    """Dedup by resource URL across rails. Keep rails/also_on. Do not drop a rail copy."""
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
            else:
                row = dict(item)
                row["rails"] = [rail]
                merged[key] = row
                order.append(key)
    return [merged[k] for k in order]


def refresh() -> dict:
    """Fetch all three rails sequentially (RAM/CPU). Keep previous items on rail error."""
    global _index, _fetched_mono
    prev = _index if isinstance(_index, dict) else _empty_index()
    prev_by = prev.get("by_rail") or {}
    prev_totals = prev.get("totals") or {}
    prev_trunc = prev.get("truncated") or {}
    by_rail: dict[str, list] = {rail: [] for rail in _RAILS}
    totals: dict = {}
    truncated: dict = {}
    errors: dict = {}

    catalogs = list(probe.CATALOGS)
    for rail, base in catalogs:
        if rail not in by_rail:
            by_rail[rail] = []
        try:
            result = fetch_rail(rail, base)
        except Exception:
            result = {"items": [], "error": "fetch_failed", "total": None, "truncated": False}
        err = result.get("error")
        got = list(result.get("items") or [])
        if err and not got:
            by_rail[rail] = list(prev_by.get(rail) or [])
            errors[rail] = err
            totals[rail] = prev_totals.get(rail)
            truncated[rail] = bool(prev_trunc.get(rail))
        else:
            by_rail[rail] = got
            totals[rail] = result.get("total")
            truncated[rail] = bool(result.get("truncated"))
            if err:
                errors[rail] = err

    items = _merge_items(by_rail)
    complete = (not any(truncated.values())) and (not errors)
    idx = {
        "items": items,
        "by_rail": by_rail,
        "fetched_at": time.time(),
        "totals": totals,
        "truncated": truncated,
        "complete": complete,
        "errors": errors,
    }
    with _lock:
        _index = idx
        _fetched_mono = time.monotonic()
    return idx


def peek_index() -> dict | None:
    """Cached index or None. Never refreshes. Pulse must not block on cold start."""
    with _lock:
        return _index


def get_index() -> dict:
    """Return cached index. Recrawl only on cold start. Daemon refresher handles TTL."""
    with _lock:
        if _index is not None:
            return _index
    with _refresh_lock:
        with _lock:
            if _index is not None:
                return _index
        return refresh()


def _refresh_loop() -> None:
    while True:
        try:
            refresh()
        except Exception:
            pass
        time.sleep(INDEX_TTL)


def start_refresher() -> None:
    """Daemon thread: refill the index every INDEX_TTL. Idempotent."""
    global _refresher_thread
    with _lock:
        if _refresher_thread is not None and _refresher_thread.is_alive():
            return
        t = threading.Thread(target=_refresh_loop, name="live402-catalog", daemon=True)
        _refresher_thread = t
        t.start()
