(function () {
  const need = document.getElementById("need");
  const form = document.getElementById("search-form");
  const searchBtn = document.getElementById("search-btn");
  const status = document.getElementById("search-status");
  const results = document.getElementById("search-results");
  const copyBtn = document.getElementById("copy-curl");
  const curlEl = document.getElementById("curl-route");
  const copyRouteBtn = document.getElementById("copy-route");
  const copyRouteJsonBtn = document.getElementById("copy-route-json");
  const routeJsonEl = document.getElementById("route-json");
  const maxPrice = document.getElementById("max-price");
  const minObservations = document.getElementById("min-observations");
  const requireInvocable = document.getElementById("require-invocable");
  const maxTotalCost = document.getElementById("max-total-cost");
  const maxCandidates = document.getElementById("max-candidates");
  const maxServiceLatency = document.getElementById("max-service-latency");
  const maxSettlementLatency = document.getElementById("max-settlement-latency");

  const RAIL_NAMES = { base: "Base", solana: "Solana", algorand: "Algorand" };
  const FORBIDDEN_RESULT_LABELS = /^(recommended|verified|live|payable now|best for|live now|verified now|best)$/i;
  const OBJECTIVES = { best: "best", cheapest: "cheapest", fastest: "fastest", most_reliable: "most_reliable" };
  const RAILS = { base: "base", solana: "solana", algorand: "algorand" };
  const DEPTHS = { standard: "standard", thorough: "thorough" };

  const policy = {
    network: "any",
    objective: "best",
    preferNetwork: "any",
    searchDepth: "standard",
  };

  function hasContent() {
    return Boolean(((need && need.value) || "").trim());
  }

  function syncSearch() {
    const ready = hasContent();
    if (searchBtn) searchBtn.disabled = !ready;
    if (copyRouteBtn) copyRouteBtn.disabled = !ready;
    if (copyRouteJsonBtn) copyRouteJsonBtn.disabled = !ready;
    renderRouteJson();
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

  function parseNonnegNumber(raw) {
    if (raw == null || String(raw).trim() === "") return null;
    const n = Number(raw);
    if (!Number.isFinite(n) || n < 0) return null;
    return n;
  }

  function parsePositiveInt(raw, min) {
    const n = parseNonnegNumber(raw);
    if (n == null) return null;
    const i = Math.floor(n);
    if (i < min) return null;
    return i;
  }

  function buildRouteBody() {
    const body = {};
    const q = ((need && need.value) || "").trim();
    if (q) body.need = q;
    if (policy.network !== "any" && RAILS[policy.network]) {
      body.networks = [policy.network];
    }
    const price = parseNonnegNumber(maxPrice && maxPrice.value);
    if (price != null) body.max_price_usd = price;
    if (requireInvocable && requireInvocable.checked) body.require_invocable = true;
    const minObs = parsePositiveInt(minObservations && minObservations.value, 0);
    if (minObs != null) body.min_observations = minObs;
    if (OBJECTIVES[policy.objective]) body.objective = policy.objective;
    if (policy.preferNetwork !== "any" && RAILS[policy.preferNetwork]) {
      body.prefer_network = policy.preferNetwork;
    }
    const total = parseNonnegNumber(maxTotalCost && maxTotalCost.value);
    if (total != null) body.max_total_cost_usd = total;
    const serviceMs = parsePositiveInt(maxServiceLatency && maxServiceLatency.value, 0);
    if (serviceMs != null) body.max_service_latency_ms = serviceMs;
    const settleMs = parsePositiveInt(maxSettlementLatency && maxSettlementLatency.value, 0);
    if (settleMs != null) body.max_settlement_latency_ms = settleMs;
    if (policy.searchDepth === "thorough") body.search_depth = "thorough";
    const candidates = parsePositiveInt(maxCandidates && maxCandidates.value, 1);
    if (candidates != null) body.max_candidates_to_probe = candidates;
    return body;
  }

  function routeJsonText() {
    return JSON.stringify(buildRouteBody(), null, 2);
  }

  function renderRouteJson() {
    if (!routeJsonEl) return;
    routeJsonEl.textContent = routeJsonText();
  }

  function previewUrl() {
    const q = ((need && need.value) || "").trim();
    let url = "/preview?need=" + encodeURIComponent(q);
    if (policy.network !== "any" && RAILS[policy.network]) {
      url += "&networks=" + encodeURIComponent(policy.network);
    }
    if (policy.preferNetwork !== "any" && RAILS[policy.preferNetwork]) {
      url += "&prefer_network=" + encodeURIComponent(policy.preferNetwork);
    }
    return url;
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
    label.textContent = "LISTED · catalog claim";
    side.appendChild(label);
    const bits = document.createElement("p");
    bits.className = "result-bits";
    const source = catalogSource(hit);
    if (source) appendBit(bits, source);
    appendBit(bits, railOf(hit));
    const price = listedPrice(hit);
    if (price) appendBit(bits, "Listed price " + price);
    const schema = schemaListed(hit);
    if (schema) appendBit(bits, "Schema listed: " + schema);
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
    label.textContent = "LAST OBSERVED · 402Signal history";
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
    if (obs.payable === true) appendBit(bits, "payable");
    else if (obs.payable === false) appendBit(bits, "not payable");
    if (obs.invocable === true) appendBit(bits, "invocable");
    else if (obs.invocable === false) appendBit(bits, "not invocable");
    const checked = isoOrEmpty(obs.last_checked);
    if (checked) appendBit(bits, "Last checked " + checked);
    const n = Number(obs.n_7d);
    if (Number.isFinite(n) && n > 0) {
      appendBit(bits, n + (n === 1 ? " observation" : " observations"));
    }
    if (obs.last_latency_ms != null && obs.last_latency_ms !== "") {
      appendBit(bits, String(obs.last_latency_ms) + " ms");
    }
    if (!bits.childNodes.length) appendBit(bits, "Prior observation on file");
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
    let text = "LISTED · " + shownLabel + " · not a live check";
    if (Number.isFinite(matches) && matches > shown) {
      const exhaustive = parsed && parsed.discovery_exhaustive === true;
      text += " · " + matches + (exhaustive ? " discovery matches" : " matches returned by discovery");
    }
    heading.textContent = text;
    results.appendChild(heading);
    const note = document.createElement("p");
    note.className = "policy-hint";
    note.textContent = "Preview shows discovery + prior observations. A paid route may differ after 402Signal checks candidates now.";
    results.appendChild(note);
    hits.forEach(function (hit) {
      results.appendChild(renderHit(hit));
    });
  }

  async function runSearch() {
    if (!hasContent()) {
      showMessage("Enter a capability to search the catalog.");
      return;
    }
    setStatus("Previewing discovery...");
    if (results) results.textContent = "";
    let pulse = null;
    try {
      const pulseRes = await fetch("/pulse", { cache: "no-store" });
      if (pulseRes.ok) pulse = await pulseRes.json();
    } catch (e) {
      pulse = null;
    }
    try {
      const res = await fetch(previewUrl(), { cache: "no-store" });
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

  async function copyText(value, button, idleLabel) {
    const label = idleLabel || "Copy";
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(value);
      } else {
        const ta = document.createElement("textarea");
        ta.value = value;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      if (button) {
        button.textContent = "Copied";
        window.setTimeout(function () { button.textContent = label; }, 1500);
      }
    } catch (e) {
      if (button) button.textContent = "Copy failed";
    }
  }

  async function copyCurl() {
    if (!curlEl) return;
    await copyText(curlEl.textContent || "", copyBtn, "Copy");
  }

  async function copyRouteRequest(button) {
    if (!hasContent()) return;
    await copyText(routeJsonText(), button, button === copyRouteBtn ? "Copy live route request" : "Copy");
  }

  function bindChipGroup(id, attr, key, allowed) {
    const root = document.getElementById(id);
    if (!root) return;
    root.addEventListener("click", function (ev) {
      const btn = ev.target.closest("[" + attr + "]");
      if (!btn) return;
      const value = btn.getAttribute(attr) || "";
      if (allowed && !allowed[value] && value !== "any") return;
      policy[key] = value;
      root.querySelectorAll(".chip").forEach(function (c) {
        const on = c === btn;
        c.classList.toggle("active", on);
        c.setAttribute("aria-pressed", on ? "true" : "false");
      });
      syncSearch();
    });
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
  [maxPrice, minObservations, requireInvocable, maxTotalCost, maxCandidates, maxServiceLatency, maxSettlementLatency].forEach(function (el) {
    if (!el) return;
    el.addEventListener("input", syncSearch);
    el.addEventListener("change", syncSearch);
  });
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
  bindChipGroup("network-chips", "data-network", "network", RAILS);
  bindChipGroup("objective-chips", "data-objective", "objective", OBJECTIVES);
  bindChipGroup("prefer-chips", "data-prefer", "preferNetwork", RAILS);
  bindChipGroup("depth-chips", "data-depth", "searchDepth", DEPTHS);
  if (copyBtn) {
    copyBtn.addEventListener("click", function (ev) {
      ev.preventDefault();
      copyCurl();
    });
  }
  if (copyRouteBtn) {
    copyRouteBtn.addEventListener("click", function (ev) {
      ev.preventDefault();
      copyRouteRequest(copyRouteBtn);
    });
  }
  if (copyRouteJsonBtn) {
    copyRouteJsonBtn.addEventListener("click", function (ev) {
      ev.preventDefault();
      copyRouteRequest(copyRouteJsonBtn);
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
