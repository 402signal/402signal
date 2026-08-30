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
    if (form) form.hidden = !ok;
    if (payBaseBtn) {
      payBaseBtn.hidden = !ok;
      if (!paying) payBaseBtn.disabled = !ok || !hasContent();
    }
    if (payBaseHint) payBaseHint.hidden = !ok;
  }

  function syncPreview() {
    const ok = Boolean(injectedWallet());
    if (form && !paying) form.hidden = !ok;
    if (payBaseBtn && !paying) {
      payBaseBtn.hidden = !ok;
      payBaseBtn.disabled = !ok || !hasContent();
    }
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
    humanBody.textContent = "GET /preview is free if you only wanted a cached look.";
  }

  function showResult(code, parsed) {
    showHuman(code, parsed);
    status.textContent = "HTTP " + code;
    if (code === 402) status.className = "http-402";
    else if (code === 200) status.className = "http-ok";
    else status.className = "http-dead";
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

  revealPayControl();
  window.addEventListener("ethereum#initialized", revealPayControl, { once: true });
  setTimeout(revealPayControl, 500);
  setTimeout(revealPayControl, 2000);
  syncPreview();
})();
