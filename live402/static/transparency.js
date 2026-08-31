(function () {
  function statusEl() {
    return document.getElementById("copy-status");
  }

  function announce(text) {
    var live = statusEl();
    if (live) live.textContent = text;
  }

  function copyText(value) {
    if (!value) return Promise.reject();
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(value);
    }
    var input = document.createElement("textarea");
    input.value = value;
    input.setAttribute("readonly", "");
    input.style.position = "absolute";
    input.style.left = "-9999px";
    document.body.appendChild(input);
    input.select();
    try {
      document.execCommand("copy");
      return Promise.resolve();
    } catch (err) {
      return Promise.reject(err);
    } finally {
      document.body.removeChild(input);
    }
  }

  document.addEventListener("click", function (event) {
    var btn = event.target.closest(".copy-btn");
    if (!btn) return;
    var value = btn.getAttribute("data-copy") || "";
    var label = btn.getAttribute("aria-label") || "Copy";
    copyText(value).then(function () {
      var prior = btn.textContent;
      btn.textContent = "Copied";
      announce("Copied " + label.replace(/^Copy\s+/i, ""));
      window.setTimeout(function () {
        btn.textContent = prior;
        announce("");
      }, 1200);
    }).catch(function () {
      announce("Copy failed");
    });
  });
})();
