(function () {
  const need = document.getElementById("need");
  const form = document.getElementById("search-form");
  const searchBtn = document.getElementById("search-btn");
  const status = document.getElementById("search-status");
  const results = document.getElementById("search-results");
  const claimsCard = document.getElementById("claims-example");
  const claimsBody = document.getElementById("claims-example-body");
  const copyBtn = document.getElementById("copy-curl");
  const curlEl = document.getElementById("curl-route");

  const RAIL_NAMES = { base: "Base", solana: "Solana", algorand: "Algorand" };
  const FORBIDDEN_RESULT_LABELS = /^(recommended|verified|live|payable now|best for)$/i;

  function hasContent() {
    return Boolean(((need && need.value) || "").trim());
  }

  function syncSearch() {
    if (searchBtn) searchBtn.disabled = !hasContent();
  }

  function unknown(v) {
    if (v === null || v === undefined || v === "") return "Unknown";
    return String(v);
  }

  function hostOf(url) {
    try {
      return new URL(url).hostname || url;
    } catch (e) {
      return url || "";
    }
  }

  function text(el, value) {
    if (el) el.textContent = value == null ? "" : String(value);
  }

  function setStatus(message) {
    text(status, message || "");
  }

  function catalogHits(parsed) {
    if (!parsed || typeof parsed !== "object") return [];
    const hits = parsed.hits;
    if (!Array.isArray(hits)) return [];
    return hits.filter(function (hit) {
      return hit && typeof hit === "object";
    });
  }

  function railOf(hit) {
    if (!hit || typeof hit !== "object") return "Unknown";
    const net = hit.chain || hit.rail || hit.network;
    if (!net) return "Unknown";
    const key = String(net).toLowerCase();
    return RAIL_NAMES[key] || String(net);
  }

  function listedPrice(hit) {
    if (!hit || typeof hit !== "object") return "Unknown";
    const claimed = hit.claimed && typeof hit.claimed === "object" ? hit.claimed : {};
    if (hit.price != null && hit.price !== "") return String(hit.price);
    if (claimed.amount != null && claimed.amount !== "") return String(claimed.amount);
    return "Unknown";
  }

  function catalogPayTo(hit) {
    if (!hit || typeof hit !== "object") return null;
    const claimed = hit.claimed && typeof hit.claimed === "object" ? hit.claimed : null;
    if (claimed && claimed.payTo) return String(claimed.payTo);
    return null;
  }

  function schemaListed(hit) {
    if (!hit || typeof hit !== "object") return "unknown";
    if (typeof hit.inputSchema_present === "boolean") {
      return hit.inputSchema_present ? "yes" : "no";
    }
    const claimed = hit.claimed && typeof hit.claimed === "object" ? hit.claimed : null;
    if (claimed && typeof claimed.schema_present === "boolean") {
      return claimed.schema_present ? "yes" : "no";
    }
    if (claimed && (claimed.schema_present === 1 || claimed.schema_present === 0)) {
      return claimed.schema_present === 1 ? "yes" : "no";
    }
    return "unknown";
  }

  function catalogSource(hit) {
    if (!hit || typeof hit !== "object") return null;
    if (typeof hit.source === "string" && hit.source) return hit.source;
    const claimed = hit.claimed && typeof hit.claimed === "object" ? hit.claimed : null;
    if (claimed && typeof claimed.source === "string" && claimed.source) return claimed.source;
    return null;
  }

  function independentlyObserved(parsed) {
    if (!parsed || typeof parsed !== "object") return null;
    const obs = parsed.observed;
    if (!obs || typeof obs !== "object") return null;
    if (obs.source && obs.source !== "402signal_observed") return null;
    const keys = Object.keys(obs).filter(function (key) {
      if (key === "source") return false;
      return obs[key] !== null && obs[key] !== undefined && obs[key] !== "";
    });
    if (!keys.length) return null;
    return obs;
  }

  function observedField(obs, key) {
    if (!obs || !Object.prototype.hasOwnProperty.call(obs, key)) return undefined;
    const value = obs[key];
    if (value === null || value === undefined || value === "") return undefined;
    return value;
  }

  function isRefreshingPulse(pulse) {
    if (!pulse || typeof pulse !== "object") return false;
    const status = String(pulse.index_status || "").toLowerCase();
    return status === "pending" || status === "refreshing";
  }

  function safeLabel(hit) {
    const url = (hit && hit.url) || "";
    const name = (hit && (hit.label || hit.need || hit.serviceName || hit.name)) || hostOf(url) || "Candidate";
    if (FORBIDDEN_RESULT_LABELS.test(String(name))) return hostOf(url) || "Candidate";
    return String(name);
  }

  function appendMeta(dl, label, value) {
    if (value == null || value === "") return;
    const row = document.createElement("div");
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = label;
    dd.textContent = value;
    row.appendChild(dt);
    row.appendChild(dd);
    dl.appendChild(row);
  }

  function renderHit(hit) {
    const article = document.createElement("article");
    article.className = "result-card";

    const title = document.createElement("h3");
    title.textContent = safeLabel(hit);
    article.appendChild(title);

    const tags = document.createElement("p");
    tags.className = "result-tags";
    const match = document.createElement("span");
    match.className = "tag";
    match.textContent = "Catalog match";
    const unverified = document.createElement("span");
    unverified.className = "tag";
    unverified.textContent = "Not live-verified";
    tags.appendChild(match);
    tags.appendChild(document.createTextNode(" "));
    tags.appendChild(unverified);
    article.appendChild(tags);

    if (hit.url) {
      const urlP = document.createElement("p");
      urlP.className = "result-url";
      urlP.textContent = String(hit.url);
      article.appendChild(urlP);
    }

    const dl = document.createElement("dl");
    dl.className = "result-meta";
    appendMeta(dl, "Listed price", listedPrice(hit));
    const payTo = catalogPayTo(hit);
    if (payTo) appendMeta(dl, "Catalog payTo", payTo);
    appendMeta(dl, "Schema listed", schemaListed(hit));
    const source = catalogSource(hit);
    if (source) appendMeta(dl, "Source", source);
    appendMeta(dl, "Rail", railOf(hit));
    const also = hit.also_on;
    if (Array.isArray(also) && also.length) {
      appendMeta(dl, "Also listed on", also.join(", "));
    }
    article.appendChild(dl);
    return article;
  }

  function showMessage(message) {
    if (!results) return;
    results.textContent = "";
    const p = document.createElement("p");
    p.className = "empty-state";
    p.id = "empty-state";
    p.textContent = message;
    results.appendChild(p);
  }

  function renderClaimsExample(parsed, hit) {
    if (!claimsCard || !claimsBody) return;
    if (!hit && !independentlyObserved(parsed)) {
      claimsCard.hidden = true;
      claimsBody.textContent = "";
      return;
    }
    const lines = [];
    if (hit) {
      lines.push("Catalog (this result)");
      lines.push("Listed price: " + listedPrice(hit));
      const payTo = catalogPayTo(hit);
      if (payTo) lines.push("Catalog payTo: " + payTo);
      else lines.push("Catalog payTo: omitted (not in this payload)");
      lines.push("Schema listed: " + schemaListed(hit));
      const source = catalogSource(hit);
      if (source) lines.push("Source: " + source);
      lines.push("Rail: " + railOf(hit));
    }
    const obs = independentlyObserved(parsed);
    if (obs) {
      lines.push("402Signal observed");
      const httpStatus = observedField(obs, "http_status");
      const obsPay = observedField(obs, "payTo");
      const schema = observedField(obs, "schema_present");
      const latency = observedField(obs, "latency_ms");
      if (httpStatus !== undefined) lines.push("HTTP status: " + unknown(httpStatus));
      if (obsPay !== undefined) lines.push("payTo: " + unknown(obsPay));
      if (schema !== undefined) {
        const schemaLabel = schema === 1 || schema === true ? "yes" : schema === 0 || schema === false ? "no" : "Unknown";
        lines.push("schema_present: " + schemaLabel);
      } else {
        lines.push("schema_present: Unknown");
      }
      if (latency !== undefined) lines.push("latency_ms: " + unknown(latency));
    } else {
      lines.push("402Signal observed: not in this response. Catalog search is not a live probe.");
    }
    claimsBody.textContent = lines.join("\n");
    claimsCard.hidden = false;
  }

  function renderResults(parsed, pulse) {
    if (!results) return;
    results.textContent = "";
    const hits = catalogHits(parsed);
    if (!hits.length) {
      if (isRefreshingPulse(pulse)) {
        showMessage("Catalog data is refreshing. Try again shortly.");
      } else {
        showMessage("No catalog matches found. Try a broader capability.");
      }
      renderClaimsExample(parsed, null);
      return;
    }
    const heading = document.createElement("h3");
    heading.className = "results-heading";
    heading.textContent = hits.length === 1 ? "1 catalog match" : hits.length + " catalog matches";
    results.appendChild(heading);
    hits.forEach(function (hit) {
      results.appendChild(renderHit(hit));
    });
    renderClaimsExample(parsed, hits[0]);
  }

  async function runSearch() {
    if (!hasContent()) {
      showMessage("Enter a capability to search the catalog.");
      return;
    }
    setStatus("Searching catalog...");
    if (results) results.textContent = "";
    if (claimsCard) claimsCard.hidden = true;
    let pulse = null;
    try {
      const pulseRes = await fetch("/pulse", { cache: "no-store" });
      if (pulseRes.ok) pulse = await pulseRes.json();
    } catch (e) {
      pulse = null;
    }
    try {
      const q = ((need && need.value) || "").trim();
      const res = await fetch("/preview?need=" + encodeURIComponent(q), { cache: "no-store" });
      const raw = await res.text();
      let parsed = null;
      try { parsed = JSON.parse(raw); } catch (e) { parsed = null; }
      if (res.status === 502 || res.status === 503) {
        showMessage("Catalog data is refreshing. Try again shortly.");
        setStatus("");
        return;
      }
      if (!res.ok) {
        showMessage("Catalog data is refreshing. Try again shortly.");
        setStatus("HTTP " + res.status);
        return;
      }
      renderResults(parsed, pulse);
      setStatus("");
    } catch (err) {
      showMessage("Catalog data is refreshing. Try again shortly.");
      setStatus("");
    }
  }

  async function copyCurl() {
    if (!curlEl) return;
    const value = curlEl.textContent || "";
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(value);
      } else {
        const range = document.createRange();
        range.selectNodeContents(curlEl);
        const sel = window.getSelection();
        if (sel) {
          sel.removeAllRanges();
          sel.addRange(range);
        }
      }
      if (copyBtn) {
        copyBtn.textContent = "Copied";
        window.setTimeout(function () { copyBtn.textContent = "Copy"; }, 1500);
      }
    } catch (e) {
      if (copyBtn) copyBtn.textContent = "Copy failed";
    }
  }

  if (form) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      runSearch();
    });
  }
  if (need) {
    need.addEventListener("input", syncSearch);
    need.addEventListener("change", syncSearch);
  }
  const chips = document.getElementById("need-chips");
  if (chips) {
    chips.addEventListener("click", function (ev) {
      const btn = ev.target.closest("[data-need]");
      if (!btn || !need) return;
      need.value = btn.getAttribute("data-need") || "";
      chips.querySelectorAll(".chip").forEach(function (c) { c.classList.remove("active"); });
      btn.classList.add("active");
      syncSearch();
    });
  }
  if (copyBtn) {
    copyBtn.addEventListener("click", function (ev) {
      ev.preventDefault();
      copyCurl();
    });
  }

  try {
    const q = new URLSearchParams(window.location.search);
    const qNeed = q.get("need");
    if (qNeed && need) {
      need.value = qNeed;
      syncSearch();
      if (hasContent()) runSearch();
    }
  } catch (e) {}

  syncSearch();
})();
