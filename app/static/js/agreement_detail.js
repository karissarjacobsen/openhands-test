// Agreement detail page: patch fields and change agreement type.
(function () {
  "use strict";

  var patchForm = document.getElementById("patch-form");
  if (patchForm) {
    patchForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      var id = patchForm.dataset.id;
      var body = {
        title: document.getElementById("patch-title").value,
        summary: document.getElementById("patch-summary").value,
        status: document.getElementById("patch-status").value,
      };
      var response = await fetch("/api/agreements/" + id, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      var payload = await response.json().catch(function () {
        return { ok: false, error: "Unexpected server response" };
      });
      if (payload.ok) {
        window.App.toast("Agreement patched.", "ok");
        setTimeout(function () { window.location.reload(); }, 800);
      } else {
        window.App.toast(payload.error || "Patch failed.", "error");
      }
    });
  }

  var typeForm = document.getElementById("change-type-form");
  if (typeForm) {
    typeForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      var id = typeForm.dataset.id;
      var newType = document.getElementById("new-type").value.trim();
      var response = await fetch("/api/agreements/" + id + "/change-type", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: newType }),
      });
      var payload = await response.json().catch(function () {
        return { ok: false, error: "Unexpected server response" };
      });
      if (payload.ok) {
        window.App.toast("Agreement type changed.", "ok");
        setTimeout(function () { window.location.reload(); }, 800);
      } else {
        window.App.toast(payload.error || "Change type failed.", "error");
      }
    });
  }
})();
