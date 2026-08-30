/* CSP-safe Pera WalletConnect v1. Bridges from wc.perawallet.app/config.json. No keys. */
(function (root) {
  "use strict";
  var BRIDGES = [
    "https://wallet-connect-a.perawallet.app",
    "https://wallet-connect-b.perawallet.app",
    "https://wallet-connect-c.perawallet.app",
    "https://wallet-connect-d.perawallet.app",
    "https://wallet-connect-e.perawallet.app",
    "https://wallet-connect-f.perawallet.app",
    "https://wallet-connect-g.perawallet.app",
    "https://wallet-connect-h.perawallet.app"
  ];
  var CHAIN_ID = 416001;
  var STORE_KEY = "pera-wc-session";

  function bytesToHex(bytes) {
    var hex = "";
    for (var i = 0; i < bytes.length; i++) hex += bytes[i].toString(16).padStart(2, "0");
    return hex;
  }
  function hexToBytes(hex) {
    var clean = String(hex || "").replace(/^0x/, "");
    var out = new Uint8Array(clean.length / 2);
    for (var i = 0; i < out.length; i++) out[i] = parseInt(clean.substr(i * 2, 2), 16);
    return out;
  }
  function randomHex(nBytes) {
    return bytesToHex(crypto.getRandomValues(new Uint8Array(nBytes)));
  }
  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    var b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    var h = bytesToHex(b);
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
  }
  function payloadId() {
    return Date.now() * 1000 + Math.floor(Math.random() * 1000);
  }
  function isIOS() {
    return /iPhone|iPad|iPod/i.test(navigator.userAgent || "");
  }
  function isMobile() {
    return /iPhone|iPad|iPod|Android/i.test(navigator.userAgent || "");
  }
  async function importAes(keyBytes, usage) {
    return crypto.subtle.importKey("raw", keyBytes, { name: "AES-CBC" }, false, usage);
  }
  async function importHmac(keyBytes, usage) {
    return crypto.subtle.importKey("raw", keyBytes, { name: "HMAC", hash: "SHA-256" }, false, usage);
  }
  async function encrypt(obj, keyHex) {
    var key = hexToBytes(keyHex);
    var iv = crypto.getRandomValues(new Uint8Array(16));
    var aes = await importAes(key, ["encrypt"]);
    var data = new TextEncoder().encode(JSON.stringify(obj));
    var cipher = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-CBC", iv: iv }, aes, data));
    var unsigned = new Uint8Array(cipher.length + iv.length);
    unsigned.set(cipher, 0);
    unsigned.set(iv, cipher.length);
    var hmacKey = await importHmac(key, ["sign"]);
    var hmac = new Uint8Array(await crypto.subtle.sign("HMAC", hmacKey, unsigned));
    return { data: bytesToHex(cipher), hmac: bytesToHex(hmac), iv: bytesToHex(iv) };
  }
  async function decrypt(payload, keyHex) {
    if (!payload || !payload.data || !payload.hmac || !payload.iv) return null;
    var key = hexToBytes(keyHex);
    var cipher = hexToBytes(payload.data);
    var iv = hexToBytes(payload.iv);
    var hmac = hexToBytes(payload.hmac);
    var unsigned = new Uint8Array(cipher.length + iv.length);
    unsigned.set(cipher, 0);
    unsigned.set(iv, cipher.length);
    var hmacKey = await importHmac(key, ["verify"]);
    var ok = await crypto.subtle.verify("HMAC", hmacKey, hmac, unsigned);
    if (!ok) return null;
    var aes = await importAes(key, ["decrypt"]);
    var plain = await crypto.subtle.decrypt({ name: "AES-CBC", iv: iv }, aes, cipher);
    try { return JSON.parse(new TextDecoder().decode(plain)); } catch (e) { return null; }
  }
  function b64ToBytes(b64) {
    var bin = atob(String(b64).replace(/-/g, "+").replace(/_/g, "/"));
    var out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  function encodeUnsigned(txn) {
    var sdk = root.algosdk;
    if (!sdk || typeof sdk.encodeUnsignedTransaction !== "function") {
      throw new Error("algosdk failed to load");
    }
    return sdk.encodeUnsignedTransaction(txn);
  }
  function bytesToB64(bytes) {
    var u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    var bin = "";
    for (var i = 0; i < u8.length; i++) bin += String.fromCharCode(u8[i]);
    return btoa(bin);
  }

  function PeraWalletConnect(opts) {
    opts = opts || {};
    this.chainId = opts.chainId || CHAIN_ID;
    this.shouldShowSignTxnToast = opts.shouldShowSignTxnToast;
    this.bridge = BRIDGES[0];
    this.accounts = [];
    this.connected = false;
    this.connector = {
      on: function (ev, fn) {
        if (!this._ons) this._ons = {};
        this._ons[ev] = fn;
      }.bind(this)
    };
    this._ons = {};
    this._pending = {};
    this._ws = null;
    this._key = "";
    this._clientId = "";
    this._peerId = "";
    this._handshakeTopic = "";
    this._handshakeId = 0;
    this._openPromise = null;
    this.lastDeeplink = "";
    this._launched = false;
    var self = this;
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") self._ensureSocket();
    });
    window.addEventListener("pageshow", function () { self._ensureSocket(); });
  }

  PeraWalletConnect.prototype._uri = function () {
    return "wc:" + this._handshakeTopic + "@1?bridge=" + encodeURIComponent(this.bridge) + "&key=" + this._key;
  };

  PeraWalletConnect.prototype._wsUrl = function () {
    var u = this.bridge.replace(/^https:/, "wss:").replace(/^http:/, "ws:");
    var host = "";
    try { host = location.host || ""; } catch (e) {}
    return u + "/?protocol=wc&version=1&env=browser&host=" + encodeURIComponent(host);
  };

  PeraWalletConnect.prototype._ensureSocket = function () {
    var self = this;
    if (this._ws && (this._ws.readyState === 0 || this._ws.readyState === 1)) {
      return this._openPromise || Promise.resolve();
    }
    this._openPromise = new Promise(function (resolve, reject) {
      var ws;
      try { ws = new WebSocket(self._wsUrl()); } catch (err) {
        reject(err);
        return;
      }
      self._ws = ws;
      var opened = false;
      ws.onopen = function () {
        opened = true;
        self._sub(self._clientId);
        if (self._handshakeTopic) self._sub(self._handshakeTopic);
        resolve();
      };
      ws.onerror = function () {
        if (!opened) reject(new Error("Pera WalletConnect bridge failed. Try again."));
      };
      ws.onclose = function () { self._ws = null; };
      ws.onmessage = function (ev) { self._onMessage(ev); };
    });
    return this._openPromise;
  };

  PeraWalletConnect.prototype._sub = function (topic) {
    if (!topic || !this._ws || this._ws.readyState !== 1) return;
    this._ws.send(JSON.stringify({ topic: topic, type: "sub", payload: "", silent: true }));
  };

  PeraWalletConnect.prototype._pub = async function (topic, obj, silent) {
    await this._ensureSocket();
    var enc = await encrypt(obj, this._key);
    this._ws.send(JSON.stringify({
      topic: topic,
      type: "pub",
      payload: JSON.stringify(enc),
      silent: !!silent
    }));
  };

  PeraWalletConnect.prototype._ack = function (topic) {
    if (!topic || !this._ws || this._ws.readyState !== 1) return;
    this._ws.send(JSON.stringify({ topic: topic, type: "ack", payload: "", silent: true }));
  };

  PeraWalletConnect.prototype._onMessage = async function (ev) {
    var msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (!msg || msg.type === "ack" || msg.type === "sub") return;
    this._ack(msg.topic);
    if (!msg.payload) return;
    var payload = msg.payload;
    if (typeof payload === "string") {
      try { payload = JSON.parse(payload); } catch (e) { return; }
    }
    var decoded = await decrypt(payload, this._key);
    if (!decoded) return;
    if (decoded.id && this._pending[decoded.id]) {
      var wait = this._pending[decoded.id];
      delete this._pending[decoded.id];
      if (decoded.error) wait.reject(new Error((decoded.error && decoded.error.message) || "WalletConnect error"));
      else wait.resolve(decoded.result);
      return;
    }
    if (decoded.method === "wc_sessionUpdate" && decoded.params && decoded.params[0] && decoded.params[0].approved === false) {
      this.connected = false;
      this.accounts = [];
      try { sessionStorage.removeItem(STORE_KEY); } catch (e) {}
      if (this._ons.disconnect) this._ons.disconnect();
    }
  };

  PeraWalletConnect.prototype._wait = function (id, ms) {
    var self = this;
    return new Promise(function (resolve, reject) {
      var t = setTimeout(function () {
        delete self._pending[id];
        reject(new Error("Pera did not respond. Return to this tab after confirming in Pera."));
      }, ms || 180000);
      self._pending[id] = {
        resolve: function (v) { clearTimeout(t); resolve(v); },
        reject: function (e) { clearTimeout(t); reject(e); }
      };
    });
  };

  PeraWalletConnect.prototype._openPera = function (uri) {
    var link = "perawallet-wc://wc?uri=" + encodeURIComponent(uri);
    this.lastDeeplink = link;
    if (isIOS() || isMobile()) {
      try { window.location.href = link; } catch (e) {}
    }
    return link;
  };

  PeraWalletConnect.prototype.beginConnect = function () {
    this._key = randomHex(32);
    this._clientId = uuid();
    this._handshakeTopic = uuid();
    this._peerId = "";
    this.connected = false;
    this._launched = true;
    return this._openPera(this._uri());
  };

  PeraWalletConnect.prototype._persist = function () {
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify({
        bridge: this.bridge,
        key: this._key,
        clientId: this._clientId,
        peerId: this._peerId,
        accounts: this.accounts,
        handshakeTopic: this._handshakeTopic,
        handshakeId: this._handshakeId,
        chainId: this.chainId
      }));
    } catch (e) {}
  };

  PeraWalletConnect.prototype._restore = function () {
    try {
      var raw = sessionStorage.getItem(STORE_KEY);
      if (!raw) return false;
      var s = JSON.parse(raw);
      if (!s || !s.key || !s.clientId || !s.accounts || !s.accounts.length) return false;
      this.bridge = s.bridge || BRIDGES[0];
      this._key = s.key;
      this._clientId = s.clientId;
      this._peerId = s.peerId || "";
      this.accounts = s.accounts;
      this._handshakeTopic = s.handshakeTopic || "";
      this._handshakeId = s.handshakeId || 0;
      this.chainId = s.chainId || this.chainId;
      this.connected = true;
      this.connector.accounts = this.accounts;
      return true;
    } catch (e) {
      return false;
    }
  };

  PeraWalletConnect.prototype.reconnectSession = async function () {
    if (this.connected && this.accounts.length) return this.accounts;
    if (!this._restore()) return [];
    try { await this._ensureSocket(); } catch (e) { return this.accounts; }
    return this.accounts;
  };

  PeraWalletConnect.prototype.connect = async function () {
    if (this.connected && this.accounts.length) return this.accounts;
    if (!this._launched) {
      var restored = await this.reconnectSession();
      if (restored && restored.length) return restored;
    }
    var lastErr = null;
    for (var i = 0; i < BRIDGES.length; i++) {
      this.bridge = BRIDGES[i];
      try {
        return await this._connectOnce();
      } catch (err) {
        lastErr = err;
        this._ws = null;
        this._openPromise = null;
      }
    }
    throw lastErr || new Error("Could not reach a Pera WalletConnect bridge.");
  };

  PeraWalletConnect.prototype._connectOnce = async function () {
    if (!this._key || !this._handshakeTopic) {
      this._key = randomHex(32);
      this._clientId = uuid();
      this._handshakeTopic = uuid();
      this._peerId = "";
      this.connected = false;
      this._launched = true;
    }
    this._openPera(this._uri());
    await this._ensureSocket();
    var request = {
      id: payloadId(),
      jsonrpc: "2.0",
      method: "wc_sessionRequest",
      params: [{
        peerId: this._clientId,
        peerMeta: {
          name: "402Signal",
          description: "Pay $0.01 USDC for a live URL or an honest miss.",
          url: "https://402signal.com",
          icons: []
        },
        chainId: this.chainId
      }]
    };
    this._handshakeId = request.id;
    var wait = this._wait(request.id, 180000);
    await this._pub(this._handshakeTopic, request, false);
    var result = await wait;
    if (!result || result.approved === false) {
      throw new Error("Pera connection rejected.");
    }
    var accounts = result.accounts || [];
    if (!accounts.length) throw new Error("Pera did not share an account.");
    this._peerId = result.peerId || "";
    this.accounts = accounts;
    this.connected = true;
    this.connector.accounts = accounts;
    this._persist();
    if (this._ons.connect) this._ons.connect(null, { accounts: accounts });
    return accounts;
  };

  PeraWalletConnect.prototype.disconnect = async function () {
    try {
      if (this.connected && this._peerId) {
        await this._pub(this._peerId, {
          id: payloadId(),
          jsonrpc: "2.0",
          method: "wc_sessionUpdate",
          params: [{ approved: false, chainId: this.chainId, accounts: [] }]
        }, true);
      }
    } catch (e) {}
    this.connected = false;
    this.accounts = [];
    this._launched = false;
    try { sessionStorage.removeItem(STORE_KEY); } catch (e) {}
    try { if (this._ws) this._ws.close(); } catch (e) {}
    this._ws = null;
    if (this._ons.disconnect) this._ons.disconnect();
  };

  PeraWalletConnect.prototype.signTransaction = async function (txGroups, signerAddress) {
    if (!this.connected || !this._peerId) {
      throw new Error("Pera is not connected. Tap Pay again to connect.");
    }
    await this._ensureSocket();
    var params = [];
    var groups = txGroups || [];
    for (var g = 0; g < groups.length; g++) {
      var group = groups[g] || [];
      for (var i = 0; i < group.length; i++) {
        var item = group[i] || {};
        var txn = item.txn;
        var encoded = {
          txn: bytesToB64(encodeUnsigned(txn))
        };
        if (item.message) encoded.message = item.message;
        if (Array.isArray(item.signers)) encoded.signers = item.signers;
        else if (signerAddress && txn && txn.from && txn.from !== signerAddress && txn.sender !== signerAddress) {
          encoded.signers = [];
        }
        params.push(encoded);
      }
    }
    var request = {
      id: payloadId(),
      jsonrpc: "2.0",
      method: "algo_signTxn",
      params: [params]
    };
    var wait = this._wait(request.id, 180000);
    await this._pub(this._peerId, request, false);
    if (isIOS() || isMobile()) {
      this.lastDeeplink = "perawallet-wc://";
      try {
        var el = document.getElementById("open-pera");
        if (el) el.href = this.lastDeeplink;
      } catch (e) {}
      try { window.location.href = "perawallet-wc://"; } catch (e) {}
    }
    var result = await wait;
    var list = Array.isArray(result) ? result.filter(Boolean) : [];
    return list.map(function (item) {
      if (item instanceof Uint8Array) return item;
      if (typeof item === "string") return b64ToBytes(item);
      if (Array.isArray(item)) return Uint8Array.from(item);
      return b64ToBytes(String(item));
    });
  };

  root.PeraWalletConnect = PeraWalletConnect;
})(window);
