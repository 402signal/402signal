/* lute-connect 2.0.1 IIFE. Popup to https://lute.app; postMessage only. No keys. */
(function (root) {
  "use strict";
  function SignTxnsError(message, code, data) {
    var err = new Error(message);
    err.name = "SignTxnsError";
    err.code = code;
    err.data = data;
    return err;
  }
  var BASE_URL = "https://lute.app";
  function popupParams() {
    var left = 100 + (window.screenX || 0);
    var top = 100 + (window.screenY || 0);
    return "width=500,height=750,left=" + left + ",top=" + top;
  }
  function LuteConnect(siteName) {
    this.siteName = siteName || document.title || "402Signal";
    this.forceWeb = false;
  }
  LuteConnect.prototype.connect = function (genesisID) {
    var self = this;
    return new Promise(function (resolve, reject) {
      var useExt = self.forceWeb ? false : Boolean(window.lute);
      var win = null;
      if (useExt) {
        window.dispatchEvent(new CustomEvent("lute-connect", {
          detail: { action: "connect", genesisID: genesisID }
        }));
      } else {
        win = window.open(BASE_URL + "/connect", self.siteName, popupParams());
      }
      var type = useExt ? "connect-response" : "message";
      function messageHandler(event) {
        if (!useExt && event.origin !== BASE_URL) return;
        var data = event.data || event.detail || {};
        switch (data.action) {
          case "ready":
            if (win) win.postMessage({ action: "network", genesisID: genesisID }, "*");
            break;
          case "connect":
            window.removeEventListener(type, messageHandler);
            resolve(data.addrs);
            break;
          case "error":
            window.removeEventListener(type, messageHandler);
            reject(new Error(data.message || "Lute error"));
            break;
          case "close":
            window.removeEventListener(type, messageHandler);
            reject(new Error("Operation Cancelled"));
            break;
        }
      }
      window.addEventListener(type, messageHandler);
    });
  };
  LuteConnect.prototype.signTxns = function (txns) {
    var self = this;
    return new Promise(function (resolve, reject) {
      var useExt = self.forceWeb ? false : Boolean(window.lute);
      var win = null;
      if (useExt) {
        window.dispatchEvent(new CustomEvent("lute-connect", {
          detail: { action: "sign", txns: txns }
        }));
      } else {
        win = window.open(BASE_URL + "/sign", self.siteName, popupParams());
      }
      var type = useExt ? "sign-txns-response" : "message";
      function messageHandler(event) {
        if (!useExt && event.origin !== BASE_URL) return;
        var detail = event.data || event.detail || {};
        switch (detail.action) {
          case "ready":
            if (win) win.postMessage({ action: "sign", txns: txns }, "*");
            break;
          case "signed":
            window.removeEventListener(type, messageHandler);
            resolve(detail.txns);
            break;
          case "error":
            window.removeEventListener(type, messageHandler);
            reject(SignTxnsError(detail.message || "Lute sign error", detail.code || 4300));
            break;
          case "close":
            window.removeEventListener(type, messageHandler);
            reject(SignTxnsError("User Rejected Request", 4100));
            break;
        }
      }
      window.addEventListener(type, messageHandler);
    });
  };
  root.LuteConnect = LuteConnect;
})(window);
