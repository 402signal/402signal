(function () {
  const need = document.getElementById("need");
  const form = document.getElementById("search-form");
  const searchBtn = document.getElementById("search-btn");
  const status = document.getElementById("search-status");
  const results = document.getElementById("search-results");
  const copyBtn = document.getElementById("copy-curl");
  const curlEl = document.getElementById("curl-route");
  const copyRouteJsonBtn = document.getElementById("copy-route-json");
  const copyRouteCurlBtn = document.getElementById("copy-route-curl");
  const routeJsonEl = document.getElementById("route-json");
  const routeCurlEl = document.getElementById("route-curl");
  const policySummaryEl = document.getElementById("policy-summary");
  const policyFactsEl = document.getElementById("policy-facts");
  const maxPrice = document.getElementById("max-price");
  const minObservations = document.getElementById("min-observations");
  const requireInvocable = document.getElementById("require-invocable");
  const maxTotalCost = document.getElementById("max-total-cost");
  const maxLatency = document.getElementById("max-latency");

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
    if (copyRouteJsonBtn) copyRouteJsonBtn.disabled = !ready;
    if (copyRouteCurlBtn) copyRouteCurlBtn.disabled = !ready;
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
    if (policy.objective !== "best" && OBJECTIVES[policy.objective]) {
      body.objective = policy.objective;
    }
    if (policy.preferNetwork !== "any" && RAILS[policy.preferNetwork]) {
      body.prefer_network = policy.preferNetwork;
    }
    const total = parseNonnegNumber(maxTotalCost && maxTotalCost.value);
    if (total != null) body.max_total_cost_usd = total;
    const latencyMs = parsePositiveInt(maxLatency && maxLatency.value, 0);
    if (latencyMs != null) body.max_latency_ms = latencyMs;
    if (policy.searchDepth === "thorough") body.search_depth = "thorough";
    return body;
  }

  function formatUsd(n) {
    const raw = String(n);
    if (!raw.includes(".")) return raw;
    return raw.replace(/\.?0+$/, "");
  }

  function joinOr(parts) {
    if (parts.length <= 1) return parts[0] || "";
    if (parts.length === 2) return parts[0] + " or " + parts[1];
    return parts.slice(0, -1).join(", ") + ", or " + parts[parts.length - 1];
  }

  function policyFacts() {
    const q = ((need && need.value) || "").trim();
    if (!q) return [];
    const rows = [];
    rows.push(["Capability", q]);
    if (policy.network !== "any" && RAIL_NAMES[policy.network]) {
      rows.push(["Network", RAIL_NAMES[policy.network] + " (required)"]);
    } else {
      rows.push(["Network", "any supported rail"]);
    }
    const price = parseNonnegNumber(maxPrice && maxPrice.value);
    if (price != null) rows.push(["Maximum merchant price", "$" + formatUsd(price)]);
    if (requireInvocable && requireInvocable.checked) {
      rows.push(["Require input schema", "yes"]);
    }
    const minObs = parsePositiveInt(minObservations && minObservations.value, 0);
    if (minObs != null) rows.push(["Minimum observations", String(minObs)]);
    const objLabel = {
      best: "default ranking",
      cheapest: "lowest price among currently probed eligible candidates",
      fastest: "lowest this-request probe RTT",
      most_reliable: "observed reliability among currently probed eligible candidates",
    };
    rows.push(["Objective", objLabel[policy.objective] || policy.objective]);
    if (policy.preferNetwork !== "any" && RAIL_NAMES[policy.preferNetwork]) {
      rows.push(["Prefer network", RAIL_NAMES[policy.preferNetwork] + " (ranking only)"]);
    }
    const total = parseNonnegNumber(maxTotalCost && maxTotalCost.value);
    if (total != null) rows.push(["Maximum total cost", "$" + formatUsd(total)]);
    const latencyMs = parsePositiveInt(maxLatency && maxLatency.value, 0);
    if (latencyMs != null) rows.push(["Maximum probe latency", latencyMs + " ms"]);
    if (policy.searchDepth === "thorough") rows.push(["Search depth", "thorough"]);
    return rows;
  }

  function policySummary() {
    const q = ((need && need.value) || "").trim();
    if (!q) return "Enter a capability above to generate the request body.";
    const clauses = [];
    clauses.push("POST /route will search for " + q + " services");
    if (policy.network !== "any" && RAIL_NAMES[policy.network]) {
      clauses.push("require a current " + RAIL_NAMES[policy.network] + " payment option");
    } else {
      clauses.push("search Base, Solana, and Algorand");
    }
    const price = parseNonnegNumber(maxPrice && maxPrice.value);
    if (price != null) clauses.push("reject observed prices above $" + formatUsd(price));
    if (requireInvocable && requireInvocable.checked) {
      clauses.push("require invocation metadata");
    }
    const minObs = parsePositiveInt(minObservations && minObservations.value, 0);
    if (minObs != null) {
      clauses.push("require at least " + minObs + (minObs === 1 ? " prior observation" : " prior observations"));
    }
    if (policy.preferNetwork !== "any" && RAIL_NAMES[policy.preferNetwork]) {
      clauses.push("rank " + RAIL_NAMES[policy.preferNetwork] + " first without locking other rails");
    }
    const total = parseNonnegNumber(maxTotalCost && maxTotalCost.value);
    if (total != null) {
      clauses.push("cap merchant price plus known fees at $" + formatUsd(total));
    }
    const latencyMs = parsePositiveInt(maxLatency && maxLatency.value, 0);
    if (latencyMs != null) {
      clauses.push("drop live hits whose known probe RTT exceeds " + latencyMs + " ms");
    }
    if (policy.searchDepth === "thorough") {
      clauses.push("use a thorough probe plan");
    }
    if (policy.objective === "cheapest") {
      clauses.push("rank the eligible probed candidates by known merchant price");
    } else if (policy.objective === "fastest") {
      clauses.push("rank the eligible probed candidates by this-request probe RTT");
    } else if (policy.objective === "most_reliable") {
      clauses.push("rank the eligible probed candidates by stronger observed history");
    }
    if (clauses.length === 1) return clauses[0] + ".";
    const head = clauses[0];
    const tail = clauses.slice(1);
    if (tail.length === 1) return head + " and " + tail[0] + ".";
    return head + ", " + tail.slice(0, -1).join(", ") + ", and " + tail[tail.length - 1] + ".";
  }

  const EMPTY_REQUEST = "Enter a capability above to generate the request body.";

  function routeJsonText() {
    if (!hasContent()) return EMPTY_REQUEST;
    return JSON.stringify(buildRouteBody(), null, 2);
  }

  function routeCurlText() {
    const compact = JSON.stringify(buildRouteBody());
    return "curl -sS -D - https://402signal.com/route -H 'Content-Type: application/json' -d '" + compact + "'";
  }

  function renderRouteJson() {
    if (routeJsonEl) routeJsonEl.textContent = routeJsonText();
    if (routeCurlEl) routeCurlEl.textContent = routeCurlText();
    if (policySummaryEl) policySummaryEl.textContent = policySummary();
    if (policyFactsEl) {
      policyFactsEl.textContent = "";
      policyFacts().forEach(function (row) {
        const line = document.createElement("p");
        const dt = document.createElement("span");
        dt.className = "fact-label";
        dt.textContent = row[0];
        line.appendChild(dt);
        line.appendChild(document.createTextNode(": " + row[1]));
        policyFactsEl.appendChild(line);
      });
    }
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
    label.textContent = "DISCOVERY LISTING";
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
    label.textContent = "PRIOR 402SIGNAL OBSERVATION";
    side.appendChild(label);
    const bits = document.createElement("p");
    bits.className = "result-bits";
    const obs = observationOf(hit);
    const status = obs && typeof obs.status === "string" ? obs.status : "not_yet_observed";
    if (!obs || status === "not_yet_observed") {
      appendBit(bits, "No prior 402Signal observation");
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
    let text = "DISCOVERY LISTING · " + shownLabel + " · not a live check";
    if (Number.isFinite(matches) && matches > shown) {
      const exhaustive = parsed && parsed.discovery_exhaustive === true;
      text += " · " + matches + (exhaustive ? " discovery matches" : " matches returned by discovery");
    }
    heading.textContent = text;
    results.appendChild(heading);
    const note = document.createElement("p");
    note.className = "policy-hint";
    note.textContent = "Preview shows discovery listings and prior 402Signal observations. A paid route may differ after 402Signal checks candidates now.";
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
    await copyText(routeJsonText(), button, "Copy JSON");
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
      if (typeof btn.blur === "function") btn.blur();
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
  [maxPrice, minObservations, requireInvocable, maxTotalCost, maxLatency].forEach(function (el) {
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
  if (copyRouteJsonBtn) {
    copyRouteJsonBtn.addEventListener("click", function (ev) {
      ev.preventDefault();
      copyRouteRequest(copyRouteJsonBtn);
    });
  }
  if (copyRouteCurlBtn) {
    copyRouteCurlBtn.addEventListener("click", function (ev) {
      ev.preventDefault();
      if (!hasContent()) return;
      copyText(routeCurlText(), copyRouteCurlBtn, "Copy curl");
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
