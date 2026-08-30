(function () {
  const need = document.getElementById("need");
  const payBaseBtn = document.getElementById("pay-base");
  const payBaseHint = document.getElementById("pay-base-hint");
  const status = document.getElementById("status");
  const form = document.getElementById("route-form");
  const human = document.getElementById("human-result");
  const humanTitle = document.getElementById("human-title");
  const humanBody = document.getElementById("human-body");

  const BASE_CHAIN_ID = 8453;
  const BASE_CHAIN_HEX = "0x2105";
  const ATOMIC_AMOUNT = "10000";
  let paying = false;

  function requestBody() {
    return { need: ((need && need.value) || "").trim() };
  }

  function hasContent() {
    return Boolean(((need && need.value) || "").trim());
  }

  function injectedWallet() {
    return window.ethereum && typeof window.ethereum.request === "function"
      ? window.ethereum
      : null;
  }

  function revealPayControl() {
    const ok = Boolean(injectedWallet());
    if (form) form.hidden = false;
    if (payBaseBtn) {
      payBaseBtn.hidden = !ok;
      if (!paying) payBaseBtn.disabled = !ok || !hasContent();
    }
    if (payBaseHint) payBaseHint.hidden = !ok;
  }

  function syncPreview() {
    const ok = Boolean(injectedWallet());
    if (form && !paying) form.hidden = false;
    if (payBaseBtn && !paying) {
      payBaseBtn.hidden = !ok;
      payBaseBtn.disabled = !ok || !hasContent();
    }
    const previewBtn = document.getElementById("preview-btn");
    const probeBtn = document.getElementById("probe-btn");
    if (previewBtn) previewBtn.disabled = !hasContent();
    if (probeBtn) probeBtn.disabled = !hasContent();
  }

  function showHuman(code, parsed) {
    if (!human || !humanTitle || !humanBody) return;
    human.hidden = false;
    if (code === 402) {
      const err = parsed && parsed.error ? String(parsed.error) : "";
      if (err && err !== "Payment required") {
        humanTitle.textContent = "Payment did not go through";
        humanBody.textContent = err;
        return;
      }
      humanTitle.textContent = "This call costs $0.01 USDC";
      const q = ((need && need.value) || "").trim() || "weather";
      humanBody.textContent = "Unpaid POST /route. Sign PAYMENT-SIGNATURE and retry, or Pay $0.01 on Base if an injected wallet is present. curl -sS -D - https://402signal.com/route -H 'Content-Type: application/json' -d " + JSON.stringify({need: q});
      return;
    }
    if (code === 200 && parsed && parsed.live && parsed.url) {
      humanTitle.textContent = "Live URL found";
      humanBody.textContent = parsed.url;
      return;
    }
    if (code === 503) {
      humanTitle.textContent = "Honest miss";
      humanBody.textContent = "Nothing live matched. Same $0.01 either way. You paid for the probe report.";
      return;
    }
    if (code === 429) {
      humanTitle.textContent = "Slow down";
      humanBody.textContent = "Too many lookups from this network right now. Try again in a minute.";
      return;
    }
    if (parsed && parsed.error) {
      humanTitle.textContent = "Could not route";
      humanBody.textContent = String(parsed.error);
      return;
    }
    humanTitle.textContent = "HTTP " + code;
    humanBody.textContent = "GET /preview is free if you only wanted a cached look.";
  }

  function showResult(code, parsed) {
    showHuman(code, parsed);
    status.textContent = "HTTP " + code;
    if (code === 402) status.className = "http-402";
    else if (code === 200) status.className = "http-ok";
    else status.className = "http-dead";
    if (code === 200 && parsed && typeof parsed === "object") {
      if (parsed.url) renderRecommend(parsed, { probed: true });
      renderObserved(parsed);
      renderCompared(parsed);
    } else if (code === 503 && parsed && typeof parsed === "object") {
      renderObserved(parsed);
      renderCompared(parsed);
    } else if (code === 402) {
      const obs = document.getElementById("observed-card");
      if (obs) obs.hidden = true;
      const cmp = document.getElementById("compared-card");
      if (cmp) cmp.hidden = true;
    }
  }

  function showClientError(message) {
    status.textContent = "wallet";
    status.className = "http-dead";
    showHuman(0, { error: message });
  }

  function pickBaseAccept(required) {
    const accepts = (required && required.accepts) || [];
    for (let i = 0; i < accepts.length; i++) {
      const item = accepts[i];
      const net = String((item && item.network) || "").toLowerCase();
      if (net === "base" || net === "eip155:8453" || net.indexOf("eip155:8453") === 0) {
        return item;
      }
    }
    return null;
  }

  function randomNonce() {
    const bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    let hex = "0x";
    for (let i = 0; i < bytes.length; i++) {
      hex += bytes[i].toString(16).padStart(2, "0");
    }
    return hex;
  }

  function encodePaymentSignature(payload) {
    const json = JSON.stringify(payload);
    const bytes = new TextEncoder().encode(json);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) {
      bin += String.fromCharCode(bytes[i]);
    }
    return btoa(bin);
  }

  async function ensureBase(eth) {
    const raw = await eth.request({ method: "eth_chainId" });
    const current = parseInt(raw, 16);
    if (current === BASE_CHAIN_ID) return;
    try {
      await eth.request({
        method: "wallet_switchEthereumChain",
        params: [{ chainId: BASE_CHAIN_HEX }],
      });
    } catch (err) {
      const code = err && err.code;
      if (code === 4902) {
        try {
          await eth.request({
            method: "wallet_addEthereumChain",
            params: [{
              chainId: BASE_CHAIN_HEX,
              chainName: "Base",
              nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
              rpcUrls: ["https://mainnet.base.org"],
              blockExplorerUrls: ["https://basescan.org"],
            }],
          });
        } catch (addErr) {
          throw new Error("Switch your wallet to Base (chain 8453). This page only pays Base.");
        }
      } else if (code === 4001) {
        throw new Error("Switch to Base (8453) to pay. This page does not pay other chains.");
      } else {
        throw new Error("Switch your wallet to Base (chain 8453). This page only pays Base.");
      }
    }
    const after = parseInt(await eth.request({ method: "eth_chainId" }), 16);
    if (after !== BASE_CHAIN_ID) {
      throw new Error("Wallet is not on Base (8453). Switch to Base, then try again.");
    }
  }

  async function signExactEip3009(eth, from, accept) {
    const extra = accept.extra || {};
    const name = extra.name || "USD Coin";
    const version = extra.version || "2";
    const asset = accept.asset || accept.currency;
    const amount = String(accept.amount);
    const payTo = accept.payTo;
    if (amount !== ATOMIC_AMOUNT) {
      throw new Error("Refusing to sign: amount must be 10000 atomic USDC ($0.01).");
    }
    if (!payTo) {
      throw new Error("Refusing to sign: 402 is missing payTo.");
    }
    if (!asset) {
      throw new Error("Refusing to sign: 402 is missing USDC asset.");
    }
    const timeout = Number(accept.maxTimeoutSeconds) || 60;
    const authorization = {
      from: from,
      to: payTo,
      value: amount,
      validAfter: "0",
      validBefore: String(Math.floor(Date.now() / 1000) + timeout),
      nonce: randomNonce(),
    };
    const typedData = {
      types: {
        EIP712Domain: [
          { name: "name", type: "string" },
          { name: "version", type: "string" },
          { name: "chainId", type: "uint256" },
          { name: "verifyingContract", type: "address" },
        ],
        TransferWithAuthorization: [
          { name: "from", type: "address" },
          { name: "to", type: "address" },
          { name: "value", type: "uint256" },
          { name: "validAfter", type: "uint256" },
          { name: "validBefore", type: "uint256" },
          { name: "nonce", type: "bytes32" },
        ],
      },
      primaryType: "TransferWithAuthorization",
      domain: {
        name: name,
        version: version,
        chainId: BASE_CHAIN_ID,
        verifyingContract: asset,
      },
      message: authorization,
    };
    const signature = await eth.request({
      method: "eth_signTypedData_v4",
      params: [from, JSON.stringify(typedData)],
    });
    if (!signature) {
      throw new Error("Wallet did not return a signature.");
    }
    return { authorization: authorization, signature: signature };
  }

  async function payBase() {
    if (paying) return;
    if (!hasContent()) {
      showClientError("need is required");
      return;
    }
    const eth = injectedWallet();
    if (!eth) {
      showClientError("No injected wallet. Use MetaMask, Coinbase Wallet, or Base App, or POST /route from an agent.");
      revealPayControl();
      return;
    }
    paying = true;
    if (payBaseBtn) {
      payBaseBtn.disabled = true;
      payBaseBtn.classList.add("posting");
    }
    status.textContent = "connecting wallet…";
    status.className = "muted";
    if (human) human.hidden = true;
    const body = requestBody();
    try {
      const accounts = await eth.request({ method: "eth_requestAccounts" });
      const from = accounts && accounts[0];
      if (!from) throw new Error("Wallet did not share an account.");
      await ensureBase(eth);
      status.textContent = "reading 402…";
      const challenge = await fetch("/route", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const challengeText = await challenge.text();
      let required;
      try { required = JSON.parse(challengeText); } catch (e) { required = null; }
      if (challenge.status !== 402 || !required || typeof required !== "object") {
        showResult(challenge.status, required == null ? challengeText : required);
        return;
      }
      const accept = pickBaseAccept(required);
      if (!accept) {
        throw new Error("402 did not advertise Base (eip155:8453). Refusing other rails.");
      }
      if (String(accept.amount) !== ATOMIC_AMOUNT) {
        throw new Error("Refusing to sign: amount must be 10000 atomic USDC ($0.01).");
      }
      status.textContent = "Confirm $0.01 USDC in your wallet…";
      const signed = await signExactEip3009(eth, from, accept);
      const paymentPayload = {
        x402Version: 2,
        resource: required.resource,
        accepted: accept,
        payload: {
          signature: signed.signature,
          authorization: signed.authorization,
        },
        extensions: required.extensions || {},
      };
      const header = encodePaymentSignature(paymentPayload);
      status.textContent = "paying…";
      const paid = await fetch("/route", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "PAYMENT-SIGNATURE": header,
        },
        body: JSON.stringify(body),
      });
      const paidText = await paid.text();
      let parsed;
      try { parsed = JSON.parse(paidText); } catch (e) { parsed = paidText; }
      showResult(paid.status, parsed);
    } catch (err) {
      const code = err && err.code;
      let message = err && err.message ? String(err.message) : String(err);
      if (code === 4001) message = "Signature rejected. No payment was sent.";
      showClientError(message);
    } finally {
      paying = false;
      if (payBaseBtn) payBaseBtn.classList.remove("posting");
      syncPreview();
    }
  }

  if (form) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
    });
  }
  if (payBaseBtn) {
    payBaseBtn.addEventListener("click", function (ev) {
      ev.preventDefault();
      payBase();
    });
  }
  if (need) {
    need.addEventListener("input", syncPreview);
    need.addEventListener("change", syncPreview);
  }

  try {
    const q = new URLSearchParams(window.location.search);
    const qNeed = q.get("need");
    if (qNeed && need) need.value = qNeed;
  } catch (e) {}

  function applyHomeTab() {
    const overviewEl = document.getElementById("panel-overview");
    const useEl = document.getElementById("panel-use");
    if (!overviewEl || !useEl) return;
    const use = String(window.location.hash || "").toLowerCase() === "#use";
    overviewEl.hidden = use;
    useEl.hidden = !use;
    const tabOverview = document.getElementById("tab-overview");
    const tabUse = document.getElementById("tab-use");
    if (tabOverview) tabOverview.classList.toggle("is-active", !use);
    if (tabUse) tabUse.classList.toggle("is-active", use);
  }
  applyHomeTab();
  window.addEventListener("hashchange", applyHomeTab);

  const EMPTY_STATE = "Index is filling. Preview has no hits yet. Pulse counts are 0. Probe live still 402s /route.";

  function unknown(v) {
    if (v === null || v === undefined || v === "") return "unknown";
    return String(v);
  }

  function hostOf(url) {
    try {
      return new URL(url).hostname || url;
    } catch (e) {
      return url || "";
    }
  }

  function yn(ok) {
    return ok ? "yes" : "no";
  }

  function showEmpty(text) {
    const el = document.getElementById("empty-state");
    if (el) {
      el.hidden = false;
      el.textContent = text || EMPTY_STATE;
    }
    renderRecommend(null);
    const obs = document.getElementById("observed-card");
    if (obs) obs.hidden = true;
    const cmp = document.getElementById("compared-card");
    if (cmp) cmp.hidden = true;
  }

  function hideEmpty() {
    const el = document.getElementById("empty-state");
    if (el) el.hidden = true;
  }

  function firstHit(parsed) {
    if (!parsed || typeof parsed !== "object") return null;
    const hits = parsed.hits;
    if (Array.isArray(hits) && hits.length) return hits[0];
    return null;
  }

  function railOf(hit) {
    if (!hit || typeof hit !== "object") return "unknown";
    const target = hit.target || {};
    const accepts = target.accepts || hit.accepts || [];
    let net = hit.chain || hit.rail || hit.network || target.network;
    if (!net && accepts.length && accepts[0]) net = accepts[0].network;
    return unknown(net);
  }

  function priceOf(hit) {
    if (!hit || typeof hit !== "object") return "unknown";
    const target = hit.target || {};
    const claimed = hit.claimed || {};
    return unknown(hit.price || target.displayAmount || claimed.amount || hit.amount);
  }

  function invocableOf(hit) {
    if (!hit || typeof hit !== "object") return "unknown";
    if (typeof hit.invocable === "boolean") return yn(hit.invocable);
    if (typeof hit.inputSchema_present === "boolean") return yn(hit.inputSchema_present);
    const target = hit.target || {};
    if (target.inputSchema) return "yes";
    return "unknown";
  }

  function verifiedOf(hit, opts) {
    opts = opts || {};
    if (!hit) return "not_probed";
    if (opts.probed || hit.verified_seconds_ago === 0 || hit.verified_seconds_ago) {
      const n = hit.verified_seconds_ago;
      if (n === 0 || n) return "verified " + n + "s ago";
    }
    if (hit.not_probed || opts.not_probed) return "not_probed";
    return "not_probed";
  }

  function renderRecommend(hit, opts) {
    const card = document.getElementById("recommend-card");
    const title = document.getElementById("recommend-title");
    const body = document.getElementById("recommend-body");
    const why = document.getElementById("recommend-why");
    if (!card || !title || !body || !why) return;
    if (!hit) {
      card.hidden = true;
      return;
    }
    opts = opts || {};
    card.hidden = false;
    const url = hit.url || "";
    const name = hit.label || hit.need || hit.serviceName || hit.name || hostOf(url) || "Recommended";
    title.textContent = name;
    const lines = [
      url || "unknown",
      "Price " + priceOf(hit),
      "Rail " + railOf(hit),
      "Invocable " + invocableOf(hit),
      verifiedOf(hit, opts)
    ];
    const also = hit.also_on;
    if (Array.isArray(also) && also.length) {
      lines.push("Also on " + also.join(", "));
    }
    body.textContent = lines.join("\n");
    why.textContent = whyFromProbe(hit, opts);
  }

  function whyFromProbe(parsed, opts) {
    opts = opts || {};
    if (opts.not_probed) return "Best capability match in the current index.";
    const rows = parsed && Array.isArray(parsed.compared) ? parsed.compared : [];
    const obj = parsed && parsed.objective;
    if (rows.length && (obj === "cheapest" || obj === "fastest" || obj === "most_reliable")) {
      return "Selected as " + String(obj).split("_").join(" ") + " among compared rows.";
    }
    return "Best capability match in the current index.";
  }

  function renderCompared(parsed) {
    const card = document.getElementById("compared-card");
    const body = document.getElementById("compared-body");
    if (!card || !body) return;
    if (!parsed || typeof parsed !== "object") {
      card.hidden = true;
      return;
    }
    const rows = parsed.compared;
    if (!Array.isArray(rows) || !rows.length) {
      card.hidden = true;
      return;
    }
    const lines = [];
    const obj = parsed.objective;
    if (obj === "cheapest" || obj === "fastest" || obj === "most_reliable") {
      lines.push("Objective " + obj);
    }
    rows.forEach(function (row) {
      if (!row || typeof row !== "object") return;
      const bits = [];
      bits.push(row.selected ? "selected" : "candidate");
      if (row.url) bits.push(row.url);
      if (row.rail) bits.push("rail " + row.rail);
      if (row.amount_atomic != null && row.amount_atomic !== "") bits.push("amount " + row.amount_atomic);
      if (row.latency_ms != null && row.latency_ms !== "") bits.push(row.latency_ms + "ms");
      bits.push("live " + yn(!!row.live));
      bits.push("invocable " + yn(!!row.invocable));
      lines.push(bits.join(" · "));
    });
    body.textContent = lines.join("\n");
    card.hidden = false;
  }

  function renderObserved(parsed) {
    const card = document.getElementById("observed-card");
    const body = document.getElementById("observed-body");
    if (!card || !body) return;
    if (!parsed || typeof parsed !== "object") {
      card.hidden = true;
      return;
    }
    if (parsed.accepts && !parsed.claimed && !parsed.observed && parsed.live == null) {
      card.hidden = true;
      return;
    }
    const claimed = parsed.claimed || {};
    const observed = parsed.observed || {};
    const catPrice = unknown(claimed.amount);
    const catPay = unknown(claimed.payTo);
    const obsStatus = observed.http_status != null ? observed.http_status : parsed.status;
    const http402 = obsStatus === 402 || parsed.has_402_challenge === true;
    const obsPay = observed.payTo || parsed.payTo || "";
    const payMatch = catPay !== "unknown" && obsPay && String(catPay).toLowerCase() === String(obsPay).toLowerCase();
    const schema = observed.schema_present === 1 || observed.schema_present === true || parsed.invocable === true || !!(parsed.target && parsed.target.inputSchema);
    const latency = observed.latency_ms != null ? observed.latency_ms : parsed.latency_ms;
    const verified = parsed.verified_seconds_ago;
    const flags = [];
    if (parsed.payTo_changed || (parsed.risk && parsed.risk.indexOf("payTo_changed") >= 0)) flags.push("payTo_changed");
    if (!schema) flags.push("missing schema");
    if (http402 && !obsPay) flags.push("402 without payTo");
    body.textContent = [
      "Catalog says: price " + catPrice + " / payTo " + catPay,
      "402Signal observed: HTTP 402 " + yn(http402) + " · payTo match " + yn(payMatch) + " · schema " + yn(schema) + " · latency " + unknown(latency) + "ms · verified " + unknown(verified) + "s ago"
    ].join("\n") + (flags.length ? "\nFlags: " + flags.join(", ") : "");
    card.hidden = false;
  }

  async function runPreview() {
    if (!hasContent()) {
      showEmpty(EMPTY_STATE);
      return;
    }
    status.textContent = "preview…";
    status.className = "muted";
    try {
      const res = await fetch("/preview?need=" + encodeURIComponent(((need && need.value) || "").trim()), { cache: "no-store" });
      const text = await res.text();
      let parsed;
      try { parsed = JSON.parse(text); } catch (e) { parsed = null; }
      if (res.status === 502) {
        showEmpty("Preview fetch 502. " + EMPTY_STATE);
        return;
      }
      if (!res.ok) {
        showEmpty("Preview fetch " + res.status + ". " + EMPTY_STATE);
        return;
      }
      const hit = firstHit(parsed);
      if (!hit) {
        showEmpty(EMPTY_STATE);
        return;
      }
      hideEmpty();
      if (parsed && parsed.not_probed) hit.not_probed = true;
      renderRecommend(hit, { not_probed: true });
      const obs = document.getElementById("observed-card");
      if (obs) obs.hidden = true;
      const cmp = document.getElementById("compared-card");
      if (cmp) cmp.hidden = true;
      status.textContent = "preview";
    } catch (err) {
      showEmpty("Preview fetch failed. " + EMPTY_STATE);
    }
  }

  async function runProbe() {
    if (!hasContent()) {
      showClientError("need is required");
      return;
    }
    status.textContent = "reading 402…";
    status.className = "muted";
    if (human) human.hidden = true;
    try {
      const res = await fetch("/route", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody()),
      });
      const text = await res.text();
      let parsed;
      try { parsed = JSON.parse(text); } catch (e) { parsed = text; }
      showResult(res.status, parsed);
    } catch (err) {
      showClientError(err && err.message ? String(err.message) : String(err));
    }
  }

  async function loadRails() {
    const box = document.getElementById("rail-chips");
    if (!box) return;
    box.textContent = "";
    try {
      const res = await fetch("/rails", { cache: "no-store" });
      if (!res.ok) return;
      const parsed = await res.json();
      const rails = parsed && parsed.rails;
      if (!Array.isArray(rails)) return;
      rails.forEach(function (row) {
        if (!row || typeof row !== "object") return;
        const name = row.network || row.rail || row.name;
        if (!name) return;
        const span = document.createElement("span");
        span.className = "chip rail-chip" + (row.up === false ? " down" : "");
        const bits = [String(name)];
        if (row.up === true) bits.push("up");
        else if (row.up === false) bits.push("down");
        if (row.latency_ms != null && row.latency_ms !== "") bits.push(String(row.latency_ms) + "ms");
        span.textContent = bits.join(" ");
        box.appendChild(span);
      });
    } catch (e) {}
  }

  const chips = document.getElementById("need-chips");
  if (chips) {
    chips.addEventListener("click", function (ev) {
      const btn = ev.target.closest("[data-need]");
      if (!btn || !need) return;
      need.value = btn.getAttribute("data-need") || "";
      chips.querySelectorAll(".chip").forEach(function (c) { c.classList.remove("active"); });
      btn.classList.add("active");
      syncPreview();
    });
  }
  const previewBtn = document.getElementById("preview-btn");
  if (previewBtn) previewBtn.addEventListener("click", function (ev) {
    ev.preventDefault();
    runPreview();
  });
  const probeBtn = document.getElementById("probe-btn");
  if (probeBtn) probeBtn.addEventListener("click", function (ev) {
    ev.preventDefault();
    runProbe();
  });

  revealPayControl();
  window.addEventListener("ethereum#initialized", revealPayControl, { once: true });
  setTimeout(revealPayControl, 500);
  setTimeout(revealPayControl, 2000);
  syncPreview();
  loadRails();
})();
