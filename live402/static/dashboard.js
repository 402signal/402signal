(function () {
  var CHAINS = ["base", "solana", "algorand"];
  var LABELS = {base: "Base", solana: "Solana", algorand: "Algorand"};
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function sourceLine(source) {
    source = source || {};
    if (source.ok) return "source ok · " + esc(source.host || "catalog");
    if (source.stale) {
      var age = source.age_s;
      return "source fail" + (age != null ? " · stale " + age + "s" : " · stale");
    }
    return "source fail";
  }
  function hostOf(url) {
    try {
      var u = new URL(url);
      return u.hostname || "";
    } catch (e) {
      return "";
    }
  }
  function homeHref(s) {
    s = s || {};
    var q = "?need=" + encodeURIComponent(s.need || s.label || "");
    if (s.url) q += "&url=" + encodeURIComponent(s.url);
    return "/" + q;
  }
  function columnHTML(chain, data) {
    data = data || {};
    var source = data.source || {};
    var staleCls = (!source.ok) ? " stale" : "";
    var samples = data.samples || [];
    var rows = [];
    samples.forEach(function (s) {
      if (!s) return;
      var need = s.label || s.need || "";
      var price = s.price || "";
      rows.push(
        '<a class="lookup" href="' + esc(homeHref(s)) + '">' +
        '<div class="lookup-row"><span>' + esc(need) + '</span><span class="muted">' + esc(price) + "</span></div>" +
        '<div class="lookup-host">' + esc(hostOf(s.url || "")) + "</div>" +
        "</a>"
      );
    });
    if (!rows.length) rows.push('<p class="muted">No sample lookups this snapshot.</p>');
    return (
      "<h2>" + (LABELS[chain] || chain) + "</h2>" +
      '<p class="age' + staleCls + '">' + sourceLine(source) + "</p>" +
      '<div class="lookups">' + rows.join("") + "</div>"
    );
  }
  function render(data) {
    if (!data) return;
    var updated = document.getElementById("updated-at");
    if (updated) updated.textContent = data.updated_at || "";
    var chains = data.chains || {};
    CHAINS.forEach(function (chain) {
      var col = document.getElementById("chain-" + chain);
      if (col) col.innerHTML = columnHTML(chain, chains[chain] || {});
    });
  }
  function poll() {
    fetch("/pulse", {cache: "no-store"})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(render)
      .catch(function () {});
  }
  poll();
  setInterval(poll, 20000);
})();
