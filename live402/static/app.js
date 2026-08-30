(function () {
  const need = document.getElementById("need");
  const url = document.getElementById("url");
  const go = document.getElementById("go");
  const payBaseBtn = document.getElementById("pay-base");
  const payBaseHint = document.getElementById("pay-base-hint");
  const payAlgoBtn = document.getElementById("pay-algo");
  const payAlgoHint = document.getElementById("pay-algo-hint");
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
  const ALGO_ASSET = "31566704";
  let paying = false;
  let algoSession = { kind: null, address: null, pera: null, lute: null };

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

  function algoWalletReady() {
    return Boolean(
      (window.algorand && typeof window.algorand.enable === "function") ||
      window.LuteConnect ||
      window.PeraWalletConnect
    );
  }

  function revealPayControl() {
    const ok = Boolean(injectedWallet());
    if (payBaseBtn) {
      payBaseBtn.hidden = !ok;
      if (!paying) payBaseBtn.disabled = !ok || !hasContent();
    }
    if (payBaseHint) payBaseHint.hidden = !ok;
    if (payAlgoBtn) {
      payAlgoBtn.hidden = false;
      if (!paying) payAlgoBtn.disabled = !hasContent();
    }
    if (payAlgoHint) payAlgoHint.hidden = false;
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
    if (payAlgoBtn && !paying) {
      payAlgoBtn.hidden = false;
      payAlgoBtn.disabled = !hasContent();
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
    if (payAlgoBtn) payAlgoBtn.disabled = true;
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

  function pickAlgoAccept(required) {
    const accepts = (required && required.accepts) || [];
    for (let i = 0; i < accepts.length; i++) {
      const item = accepts[i];
      const net = String((item && item.network) || "").toLowerCase();
      const asset = String((item && (item.asset || item.currency)) || "");
      const amount = String((item && item.amount) || "");
      if (net.indexOf("algorand") === 0 && asset === ALGO_ASSET && amount === ATOMIC_AMOUNT) {
        return item;
      }
    }
    return null;
  }

  function bytesToB64(bytes) {
    const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    let bin = "";
    for (let i = 0; i < u8.length; i++) bin += String.fromCharCode(u8[i]);
    return btoa(bin);
  }

  function b64ToBytes(b64) {
    const bin = atob(String(b64).replace(/-/g, "+").replace(/_/g, "/"));
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function normalizeSigned(signed) {
    if (!signed) throw new Error("Wallet did not return a signed payment.");
    if (signed instanceof Uint8Array) return bytesToB64(signed);
    if (typeof signed === "string") return signed;
    if (ArrayBuffer.isView(signed)) return bytesToB64(new Uint8Array(signed.buffer, signed.byteOffset, signed.byteLength));
    throw new Error("Unexpected signed transaction format.");
  }

  function suggestedParamsFrom(accept) {
    const extra = (accept && accept.extra) || {};
    const raw = extra.suggestedParams || extra.suggested_params || {};
    const first = Number(raw.firstValid || raw.firstRound || raw["first-round"] || 0);
    const last = Number(raw.lastValid || raw.lastRound || raw["last-round"] || 0);
    const minFee = Number(raw.minFee || raw.fee || 1000);
    if (!first || !last) {
      throw new Error("402 is missing Algorand suggestedParams. Try again in a moment.");
    }
    let gh = raw.genesisHash || raw["genesis-hash"];
    if (typeof gh === "string") gh = b64ToBytes(gh);
    if (!(gh instanceof Uint8Array) || !gh.length) {
      throw new Error("402 suggestedParams.genesisHash is missing.");
    }
    return {
      genesisHash: gh,
      genesisID: raw.genesisID || raw["genesis-id"] || "mainnet-v1.0",
      firstValid: first,
      lastValid: last,
      fee: minFee,
      minFee: minFee,
      flatFee: true,
    };
  }

  function copyAccept(accept) {
    const out = JSON.parse(JSON.stringify(accept));
    if (out.extra) {
      delete out.extra.suggestedParams;
      delete out.extra.suggested_params;
      delete out.extra.unsignedGroup;
      delete out.extra.decimals;
      delete out.extra.sender;
    }
    return out;
  }

  function isLikelyIphone() {
    return /iPhone|iPad|iPod/i.test(navigator.userAgent || "");
  }

  async function connectAlgoWallet() {
    if (algoSession.address) return algoSession.address;
    if (window.algorand && typeof window.algorand.enable === "function") {
      const res = await window.algorand.enable({ genesisID: "mainnet-v1.0" });
      const accounts = Array.isArray(res) ? res : (res && res.accounts) || [];
      if (accounts[0]) {
        algoSession = { kind: "injected", address: accounts[0], pera: null, lute: null };
        return accounts[0];
      }
    }
    if (window.LuteConnect && (window.lute || !isLikelyIphone())) {
      try {
        const lute = new window.LuteConnect("402Signal");
        const addrs = await lute.connect("mainnet-v1.0");
        if (addrs && addrs[0]) {
          algoSession = { kind: "lute", address: addrs[0], pera: null, lute: lute };
          return addrs[0];
        }
      } catch (err) {
        if (window.lute) throw err;
      }
    }
    if (!window.PeraWalletConnect) {
      throw new Error("Algorand wallet libraries failed to load.");
    }
    const pera = new window.PeraWalletConnect({ chainId: 416001, shouldShowSignTxnToast: false });
    let accounts = [];
    try { accounts = await pera.reconnectSession(); } catch (e) { accounts = []; }
    if (!accounts || !accounts.length) {
      accounts = await pera.connect();
    }
    if (!accounts || !accounts[0]) {
      throw new Error("Pera did not share an account.");
    }
    algoSession = { kind: "pera", address: accounts[0], pera: pera, lute: null };
    try {
      if (pera.connector && typeof pera.connector.on === "function") {
        pera.connector.on("disconnect", function () {
          algoSession = { kind: null, address: null, pera: null, lute: null };
        });
      }
    } catch (e) {}
    return accounts[0];
  }

  function makeAlgoGroup(from, accept) {
    const sdk = window.algosdk;
    if (!sdk) throw new Error("algosdk failed to load.");
    const extra = accept.extra || {};
    const ug = extra.unsignedGroup || {};
    if (ug.txns && ug.txns.length === 2 && typeof sdk.decodeUnsignedTransaction === "function") {
      try {
        return [
          sdk.decodeUnsignedTransaction(b64ToBytes(ug.txns[0])),
          sdk.decodeUnsignedTransaction(b64ToBytes(ug.txns[1])),
        ];
      } catch (e) {}
    }
    const feePayer = extra.feePayer;
    const payTo = accept.payTo;
    const amount = String(accept.amount);
    const asset = String(accept.asset || accept.currency || "");
    if (amount !== ATOMIC_AMOUNT) {
      throw new Error("Refusing to sign: amount must be 10000 atomic USDC ($0.01).");
    }
    if (asset !== ALGO_ASSET) {
      throw new Error("Refusing to sign: asset must be USDC ASA 31566704.");
    }
    if (!payTo) throw new Error("Refusing to sign: 402 is missing payTo.");
    if (!feePayer) throw new Error("Refusing to sign: 402 is missing extra.feePayer.");
    const sp = suggestedParamsFrom(accept);
    const feeSp = Object.assign({}, sp, { fee: (Number(sp.minFee) || 1000) * 2, flatFee: true });
    const paySp = Object.assign({}, sp, { fee: 0, flatFee: true });
    const feeNote = new TextEncoder().encode("x402-fee-payer");
    const payNote = new TextEncoder().encode("x402-payment-v2");
    let feeTxn;
    let payTxn;
    try {
      feeTxn = sdk.makePaymentTxnWithSuggestedParamsFromObject({
        sender: feePayer,
        receiver: feePayer,
        amount: 0,
        suggestedParams: feeSp,
        note: feeNote,
      });
    } catch (e) {
      feeTxn = sdk.makePaymentTxnWithSuggestedParamsFromObject({
        from: feePayer,
        to: feePayer,
        amount: 0,
        suggestedParams: feeSp,
        note: feeNote,
      });
    }
    try {
      payTxn = sdk.makeAssetTransferTxnWithSuggestedParamsFromObject({
        sender: from,
        receiver: payTo,
        amount: Number(amount),
        assetIndex: Number(ALGO_ASSET),
        suggestedParams: paySp,
        note: payNote,
      });
    } catch (e) {
      payTxn = sdk.makeAssetTransferTxnWithSuggestedParamsFromObject({
        from: from,
        to: payTo,
        amount: Number(amount),
        assetIndex: Number(ALGO_ASSET),
        suggestedParams: paySp,
        note: payNote,
      });
    }
    return sdk.assignGroupID([feeTxn, payTxn]);
  }

  async function signAlgoGroup(grouped) {
    const sdk = window.algosdk;
    const unsignedFee = bytesToB64(sdk.encodeUnsignedTransaction(grouped[0]));
    const unsignedPay = bytesToB64(sdk.encodeUnsignedTransaction(grouped[1]));
    if (algoSession.kind === "injected") {
      const signed = await window.algorand.signTxns([
        { txn: unsignedFee, signers: [] },
        { txn: unsignedPay, message: "Pay $0.01 USDC on Algorand" },
      ]);
      return [unsignedFee, normalizeSigned(signed && signed[1])];
    }
    if (algoSession.kind === "lute") {
      const signed = await algoSession.lute.signTxns([
        { txn: unsignedFee, signers: [] },
        { txn: unsignedPay },
      ]);
      return [unsignedFee, normalizeSigned(signed && signed[1])];
    }
    if (algoSession.kind === "pera") {
      const signed = await algoSession.pera.signTransaction([
        [
          { txn: grouped[0], signers: [] },
          { txn: grouped[1], message: "Pay $0.01 USDC on Algorand" },
        ],
      ]);
      const paySigned = signed && (signed.length > 1 ? signed[1] : signed[0]);
      return [unsignedFee, normalizeSigned(paySigned)];
    }
    throw new Error("No Algorand wallet is connected.");
  }

  async function payAlgo() {
    if (paying) return;
    if (!hasContent()) {
      showClientError("need or url is required");
      return;
    }
    if (!algoWalletReady()) {
      showClientError("Algorand wallet libraries failed to load. Refresh, or POST /route from an agent.");
      return;
    }
    paying = true;
    if (go) {
      go.disabled = true;
      go.classList.add("posting");
    }
    if (payBaseBtn) payBaseBtn.disabled = true;
    if (payAlgoBtn) {
      payAlgoBtn.disabled = true;
      payAlgoBtn.classList.add("posting");
    }
    status.textContent = "connecting Pera or Lute…";
    status.className = "muted";
    if (out) out.hidden = true;
    if (human) human.hidden = true;
    setOutCaption("");
    const body = requestBody();
    try {
      const from = await connectAlgoWallet();
      if (!from) throw new Error("Wallet did not share an account.");
      status.textContent = "reading 402…";
      const challenge = await fetch("/route", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Algorand-Sender": from,
        },
        body: JSON.stringify(body),
      });
      const challengeText = await challenge.text();
      let required;
      try { required = JSON.parse(challengeText); } catch (e) { required = null; }
      if (challenge.status !== 402 || !required || typeof required !== "object") {
        showResult(challenge.status, required == null ? challengeText : required);
        return;
      }
      const accept = pickAlgoAccept(required);
      if (!accept) {
        throw new Error("402 did not advertise Algorand USDC ASA 31566704 amount 10000.");
      }
      const extra = accept.extra || {};
      if (!extra.feePayer) {
        throw new Error("402 Algorand accept is missing extra.feePayer. Refusing to invent one.");
      }
      if (!extra.facilitator) {
        throw new Error("402 Algorand accept is missing extra.facilitator. Refusing to invent one.");
      }
      status.textContent = "Confirm $0.01 USDC in Pera or Lute…";
      const grouped = makeAlgoGroup(from, accept);
      const groupB64 = await signAlgoGroup(grouped);
      const paymentPayload = {
        x402Version: 2,
        scheme: accept.scheme || "exact",
        network: accept.network,
        resource: required.resource,
        accepted: copyAccept(accept),
        payload: {
          paymentIndex: 1,
          paymentGroup: groupB64,
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
      if (code === 4001 || code === 4100) message = "Signature rejected. No payment was sent.";
      if (/cancelled|canceled|rejected/i.test(message) && !/402/.test(message)) {
        message = "Signature rejected. No payment was sent.";
      }
      showClientError(message);
    } finally {
      paying = false;
      if (go) go.classList.remove("posting");
      if (payBaseBtn) payBaseBtn.classList.remove("posting");
      if (payAlgoBtn) payAlgoBtn.classList.remove("posting");
      syncPreview();
    }
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
    if (payAlgoBtn) payAlgoBtn.disabled = true;
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
  if (payAlgoBtn) {
    payAlgoBtn.addEventListener("click", function (ev) {
      ev.preventDefault();
      payAlgo();
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
