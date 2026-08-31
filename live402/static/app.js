(function () {
  const need = document.getElementById("need");
  const form = document.getElementById("search-form");
  const searchBtn = document.getElementById("search-btn");
  const status = document.getElementById("search-status");
  const results = document.getElementById("search-results");
  const copyBtn = document.getElementById("copy-curl");
  const curlEl = document.getElementById("curl-route");

  const RAIL_NAMES = { base: "Base", solana: "Solana", algorand: "Algorand" };
  const FORBIDDEN_RESULT_LABELS = /^(recommended|verified|live|payable now|best for)$/i;
  const MIN_RELIABILITY_N = 10;

  function hasContent() {
    return Boolean(((need && need.value) || "").trim());
  }

  function syncSearch() {
    if (searchBtn) searchBtn.disabled = !hasContent();
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
    if (!hit || typeof hit !== "object") return "";
    const net = hit.chain || hit.rail || hit.network;
    if (!net) return "";
    const key = String(net).toLowerCase();
    return RAIL_NAMES[key] || String(net);
  }

  function listedPrice(hit) {
    if (!hit || typeof hit !== "object") return "";
    if (hit.price != null && hit.price !== "") return String(hit.price);
    const claimed = hit.claimed && typeof hit.claimed === "object" ? hit.claimed : {};
    if (claimed.amount != null && claimed.amount !== "") return String(claimed.amount);
    return "";
  }

  function schemaListed(hit) {
    if (!hit || typeof hit !== "object") return "";
    if (typeof hit.inputSchema_present === "boolean") {
      return hit.inputSchema_present ? "Yes" : "No";
    }
    const claimed = hit.claimed && typeof hit.claimed === "object" ? hit.claimed : null;
    if (claimed && typeof claimed.schema_present === "boolean") {
      return claimed.schema_present ? "Yes" : "No";
    }
    return "";
  }

  function catalogSource(hit) {
    if (!hit || typeof hit !== "object") return "";
    if (typeof hit.source === "string" && hit.source.trim()) return hit.source.trim();
    const claimed = hit.claimed && typeof hit.claimed === "object" ? hit.claimed : null;
    if (claimed && typeof claimed.source === "string" && claimed.source.trim()) {
      return claimed.source.trim();
    }
    return "";
  }

  function isRefreshingPulse(pulse) {
    if (!pulse || typeof pulse !== "object") return false;
    const statusText = String(pulse.index_status || "").toLowerCase();
    return statusText === "pending" || statusText === "refreshing";
  }

  function safeLabel(hit) {
    const url = (hit && hit.url) || "";
    const name = (hit && (hit.label || hit.need || hit.serviceName || hit.name)) || hostOf(url) || "Candidate";
    if (FORBIDDEN_RESULT_LABELS.test(String(name))) return hostOf(url) || "Candidate";
    return String(name);
  }

  function appendBit(row, value) {
    if (!value) return;
    const span = document.createElement("span");
    span.textContent = value;
    row.appendChild(span);
  }

  function observationOf(hit) {
    if (!hit || typeof hit !== "object") return null;
    return hit.observation && typeof hit.observation === "object" ? hit.observation : null;
  }

  function isoOrEmpty(value) {
    if (value == null || value === "") return "";
    return String(value);
  }

  function renderClaimSide(hit) {
    const side = document.createElement("div");
    side.className = "result-side claim";
    const label = document.createElement("p");
    label.className = "result-side-label";
    label.textContent = "DISCOVERED · discovery claim";
    side.appendChild(label);
    const bits = document.createElement("p");
    bits.className = "result-bits";
    appendBit(bits, railOf(hit));
    const price = listedPrice(hit);
    if (price) appendBit(bits, "Listed price " + price);
    const schema = schemaListed(hit);
    if (schema) appendBit(bits, "Schema listed: " + schema);
    const source = catalogSource(hit);
    if (source) appendBit(bits, source);
    if (hit && hit.method) appendBit(bits, String(hit.method));
    if (!bits.childNodes.length) appendBit(bits, "Seller fields as listed");
    side.appendChild(bits);
    return side;
  }

  function renderObservationSide(hit) {
    const side = document.createElement("div");
    side.className = "result-side observation";
    const label = document.createElement("p");
    label.className = "result-side-label";
    label.textContent = "OBSERVED · 402Signal observation";
    side.appendChild(label);
    const bits = document.createElement("p");
    bits.className = "result-bits";
    const obs = observationOf(hit);
    const status = obs && typeof obs.status === "string" ? obs.status : "not_yet_observed";
    if (!obs || status === "not_yet_observed") {
      appendBit(bits, "Not yet observed");
      side.appendChild(bits);
      return side;
    }
    appendBit(bits, "observed");
    if (obs.payable === true) appendBit(bits, "payable");
    else if (obs.payable === false) appendBit(bits, "not payable");
    if (obs.invocable === true) appendBit(bits, "invocable");
    const checked = isoOrEmpty(obs.last_checked);
    if (checked) appendBit(bits, "Last checked " + checked);
    if (obs.last_latency_ms != null && obs.last_latency_ms !== "") {
      appendBit(bits, String(obs.last_latency_ms) + " ms");
    }
    const n7 = Number(obs.n_7d);
    if (Number.isFinite(n7) && n7 > 0) {
      if (n7 >= MIN_RELIABILITY_N && obs.success_7d != null && obs.success_7d !== "") {
        const pct = Math.round(Number(obs.success_7d) * 100);
        if (Number.isFinite(pct)) appendBit(bits, n7 + " in 7d · " + pct + "%");
        else appendBit(bits, n7 + " in 7d");
      } else {
        appendBit(bits, n7 + " in 7d");
      }
    }
    side.appendChild(bits);
    return side;
  }

  function renderHit(hit) {
    const article = document.createElement("article");
    article.className = "result-row";

    const name = document.createElement("p");
    name.className = "result-name";
    name.textContent = safeLabel(hit);
    article.appendChild(name);

    if (hit.url) {
      const urlP = document.createElement("p");
      urlP.className = "result-url";
      urlP.textContent = String(hit.url);
      article.appendChild(urlP);
    }

    const sides = document.createElement("div");
    sides.className = "result-sides";
    sides.appendChild(renderClaimSide(hit));
    sides.appendChild(renderObservationSide(hit));
    article.appendChild(sides);
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
      return;
    }
    const heading = document.createElement("p");
    heading.className = "results-heading";
    const shown = hits.length;
    const matches = Number(parsed && parsed.discovery_matches);
    const shownLabel = shown === 1 ? "1 shown" : shown + " shown";
    let text = "DISCOVERED · " + shownLabel;
    if (Number.isFinite(matches) && matches > shown) {
      const exhaustive = parsed && parsed.discovery_exhaustive === true;
      text += " · " + matches + (exhaustive ? " discovery matches" : " matches returned by discovery");
    }
    heading.textContent = text;
    results.appendChild(heading);
    hits.forEach(function (hit) {
      results.appendChild(renderHit(hit));
    });
  }

  async function runSearch() {
    if (!hasContent()) {
      showMessage("Enter a capability to search the catalog.");
      return;
    }
    setStatus("Searching catalog...");
    if (results) results.textContent = "";
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

  function bindTabs() {
    const list = document.querySelector(".seg");
    if (!list) return;
    const buttons = Array.prototype.slice.call(list.querySelectorAll("[data-tab]"));
    function show(name) {
      buttons.forEach(function (btn) {
        const on = btn.getAttribute("data-tab") === name;
        btn.classList.toggle("is-active", on);
        btn.setAttribute("aria-selected", on ? "true" : "false");
      });
      document.querySelectorAll(".tab-panel").forEach(function (panel) {
        const on = panel.id === "panel-" + name;
        if (on) panel.removeAttribute("hidden");
        else panel.setAttribute("hidden", "");
      });
    }
    list.addEventListener("click", function (ev) {
      const btn = ev.target.closest("[data-tab]");
      if (!btn) return;
      show(btn.getAttribute("data-tab") || "http");
    });
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
  bindTabs();

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
