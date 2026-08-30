(function () {
  const need = document.getElementById("need");
  const url = document.getElementById("url");
  const go = document.getElementById("go");
  const payBaseBtn = document.getElementById("pay-base");
  const payBaseHint = document.getElementById("pay-base-hint");
  const out = document.getElementById("out");
  const status = document.getElementById("status");
  const preview = document.getElementById("preview");
  const form = document.getElementById("route-form");
  const human = document.getElementById("human-result");
  const humanTitle = document.getElementById("human-title");
  const humanBody = document.getElementById("human-body");
  let outCaption = document.getElementById("out-caption");
  const chips = document.getElementById("need-chips");

  const BASE_CHAIN_ID = 8453;
  const BASE_CHAIN_HEX = "0x2105";
  const ATOMIC_AMOUNT = "10000";
  let paying = false;

  function setOutCaption(text) {
    if (!outCaption) return;
    if (text) {
      outCaption.hidden = false;
      outCaption.textContent = text;
    } else {
      outCaption.hidden = true;
      outCaption.textContent = "";
    }
  }

  function requestBody() {
    const body = { need: (need.value || "").trim() };
    const u = (url.value || "").trim();
    if (u) body.url = u;
    return body;
  }

  function hasContent() {
    return Boolean((need.value || "").trim() || (url.value || "").trim());
  }

  function markChip() {
    if (!chips || !need) return;
    const val = (need.value || "").trim();
    chips.querySelectorAll(".chip").forEach(function (btn) {
      btn.classList.toggle("active", (btn.getAttribute("data-need") || "") === val);
    });
  }

  function injectedWallet() {
    return window.ethereum && typeof window.ethereum.request === "function"
      ? window.ethereum
      : null;
  }

  function revealPayControl() {
    const ok = Boolean(injectedWallet());
    if (payBaseBtn) {
      payBaseBtn.hidden = !ok;
      if (!paying) payBaseBtn.disabled = !ok || !hasContent();
    }
    if (payBaseHint) payBaseHint.hidden = !ok;
  }

  function syncPreview() {
    if (preview) {
      preview.textContent = JSON.stringify(requestBody(), null, 2);
    }
    if (go && !go.classList.contains("posting") && !paying) {
      go.disabled = !hasContent();
    }
    if (payBaseBtn && !paying) {
      const ok = Boolean(injectedWallet());
      payBaseBtn.hidden = !ok;
      payBaseBtn.disabled = !ok || !hasContent();
    }
    markChip();
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
      humanBody.textContent = "Pay one penny. We then look for a live paid API and return its URL, or tell you if nothing is up.";
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
    humanBody.textContent = "Open technical details if you need the raw response.";
  }

  function showResult(code, parsed) {
    showHuman(code, parsed);
    if (out) {
      out.hidden = false;
      out.textContent = typeof parsed === "string" ? parsed : JSON.stringify(parsed, null, 2);
    }
    setOutCaption(code === 402 ? "Price: $0.01 USDC (10000 atomic, 6 decimals)." : "");
    status.textContent = "HTTP " + code;
    if (code === 402) status.className = "http-402";
    else if (code === 200) status.className = "http-ok";
    else status.className = "http-dead";
  }

  function showClientError(message) {
    status.textContent = "wallet";
    status.className = "http-dead";
    showHuman(0, { error: message });
    if (out) {
      out.hidden = false;
      out.textContent = message;
    }
    setOutCaption("");
  }

  async function run() {
    if (!hasContent()) {
      status.textContent = "HTTP 400";
      status.className = "http-dead";
      if (human) human.hidden = true;
      if (out) {
        out.hidden = false;
        out.textContent = JSON.stringify({ error: "need or url is required" }, null, 2);
      }
      setOutCaption("");
      syncPreview();
      return;
    }
    const body = requestBody();
    go.disabled = true;
    go.classList.add("posting");
    if (payBaseBtn) payBaseBtn.disabled = true;
    status.textContent = "looking…";
    status.className = "muted";
    if (out) out.hidden = true;
    if (human) human.hidden = true;
    setOutCaption("");
    try {
      const res = await fetch("/route", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const text = await res.text();
      let parsed;
      try { parsed = JSON.parse(text); } catch (e) { parsed = text; }
      showResult(res.status, parsed);
    } catch (err) {
      status.textContent = "request failed";
      status.className = "http-dead";
      showHuman(0, { error: String(err) });
      if (out) {
        out.hidden = false;
        out.textContent = String(err);
      }
      setOutCaption("");
    } finally {
      go.classList.remove("posting");
      syncPreview();
    }
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
      showClientError("need or url is required");
      return;
    }
    const eth = injectedWallet();
    if (!eth) {
      showClientError("No injected wallet. Use MetaMask, Coinbase Wallet, or Base App, or POST /route from an agent.");
      revealPayControl();
      return;
    }
    paying = true;
    if (go) {
      go.disabled = true;
      go.classList.add("posting");
    }
    if (payBaseBtn) {
      payBaseBtn.disabled = true;
      payBaseBtn.classList.add("posting");
    }
    status.textContent = "connecting wallet…";
    status.className = "muted";
    if (out) out.hidden = true;
    if (human) human.hidden = true;
    setOutCaption("");
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
      if (go) go.classList.remove("posting");
      if (payBaseBtn) payBaseBtn.classList.remove("posting");
      syncPreview();
    }
  }

  if (form) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      run();
    });
  } else if (go) {
    go.addEventListener("click", run);
  }
  if (payBaseBtn) {
    payBaseBtn.addEventListener("click", function (ev) {
      ev.preventDefault();
      payBase();
    });
  }
  [need, url].forEach(function (el) {
    if (!el) return;
    el.addEventListener("input", syncPreview);
    el.addEventListener("change", syncPreview);
  });
  if (chips) {
    chips.addEventListener("click", function (ev) {
      const btn = ev.target.closest(".chip");
      if (!btn || !need) return;
      need.value = btn.getAttribute("data-need") || "";
      syncPreview();
      need.focus();
    });
  }

  try {
    const q = new URLSearchParams(window.location.search);
    const qNeed = q.get("need");
    const qUrl = q.get("url");
    if (qNeed && need) need.value = qNeed;
    if (qUrl && url) url.value = qUrl;
  } catch (e) {}

  const samplesBox = document.getElementById("route-samples");
  const samplesPanel = document.getElementById("route-samples-panel");
  if (samplesBox) {
    function hideSamples() {
      if (samplesPanel) samplesPanel.hidden = true;
    }
    function renderSamples(data) {
      const list = (data && data.samples) || [];
      if (!list.length) {
        hideSamples();
        return;
      }
      samplesBox.textContent = "";
      list.forEach(function (s) {
        if (!s) return;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "chip sample";
        const label = s.label || s.need || "";
        const chain = s.chain || "";
        const price = s.price || "";
        btn.textContent = label + (chain ? " · " + chain : "") + (price ? " · " + price : "");
        btn.setAttribute("data-need", s.need || label);
        btn.setAttribute("data-url", s.url || "");
        samplesBox.appendChild(btn);
      });
      if (samplesPanel) samplesPanel.hidden = false;
    }
    samplesBox.addEventListener("click", function (ev) {
      const btn = ev.target.closest("button");
      if (!btn) return;
      if (need) need.value = btn.getAttribute("data-need") || "";
      if (url) url.value = btn.getAttribute("data-url") || "";
      syncPreview();
      if (need) need.focus();
    });
    fetch("/pulse")
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (data) {
        if (!data) {
          hideSamples();
          return;
        }
        renderSamples(data);
      })
      .catch(function () { hideSamples(); });
  }

  revealPayControl();
  window.addEventListener("ethereum#initialized", revealPayControl, { once: true });
  setTimeout(revealPayControl, 500);
  setTimeout(revealPayControl, 2000);
  syncPreview();
})();
