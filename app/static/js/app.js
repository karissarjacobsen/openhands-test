// Shared helpers: toasts, button loading state, JSON fetch wrapper.
(function () {
  "use strict";

  function toast(message, kind) {
    var el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.className = "toast" + (kind ? " " + kind : "");
    el.hidden = false;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function () { el.hidden = true; }, 5000);
  }

  function setLoading(button, loading, label) {
    if (!button) return;
    if (loading) {
      button.dataset.originalText = button.innerHTML;
      button.disabled = true;
      button.innerHTML = '<span class="spinner"></span> ' + (label || "Working…");
    } else {
      button.disabled = false;
      if (button.dataset.originalText) button.innerHTML = button.dataset.originalText;
    }
  }

  async function postJSON(url, options) {
    var response = await fetch(url, Object.assign({ method: "POST" }, options || {}));
    var payload;
    try {
      payload = await response.json();
    } catch (err) {
      payload = { ok: false, error: "Unexpected server response (" + response.status + ")" };
    }
    if (!response.ok && payload.ok !== false) {
      payload = { ok: false, error: "Request failed (" + response.status + ")" };
    }
    return payload;
  }

  window.App = { toast: toast, setLoading: setLoading, postJSON: postJSON };

  // Wire up the action buttons on the agreement detail page.
  document.addEventListener("click", async function (event) {
    var button = event.target.closest("button[data-action]");
    if (!button) return;
    var action = button.dataset.action;
    var id = button.dataset.id;
    if (!action || !id) return;

    var labels = { send: "Sending…", refresh: "Refreshing…" };
    setLoading(button, true, labels[action]);

    try {
      var payload = await postJSON("/agreements/" + id + "/" + action);
      if (payload.ok) {
        toast(
          action === "send" ? "Envelope sent." : "Status refreshed.",
          "ok"
        );
        setTimeout(function () { window.location.reload(); }, 900);
      } else {
        setLoading(button, false);
        toast(payload.error || "Something went wrong.", "error");
      }
    } catch (err) {
      setLoading(button, false);
      toast("Network error: " + err.message, "error");
    }
  });
})();
