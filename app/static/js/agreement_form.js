// New-agreement form: dynamic recipients, document source toggle, template preview.
(function () {
  "use strict";

  var recipients = document.getElementById("recipients");
  var addButton = document.getElementById("add-recipient");
  var form = document.getElementById("agreement-form");
  var sendButton = document.getElementById("send-btn");

  function recipientRow() {
    var row = document.createElement("div");
    row.className = "recipient-row";
    row.innerHTML =
      '<div><label>Name</label><input type="text" name="recipient_name" placeholder="Jane Doe" /></div>' +
      '<div><label>Email</label><input type="email" name="recipient_email" placeholder="jane@example.com" /></div>' +
      '<div><label>Role</label><select name="recipient_role">' +
      '<option value="signer">Signer</option>' +
      '<option value="carbonCopy">Carbon copy</option></select></div>' +
      '<div><button type="button" class="btn btn-sm remove-recipient">Remove</button></div>';
    return row;
  }

  addButton.addEventListener("click", function () {
    recipients.appendChild(recipientRow());
  });

  recipients.addEventListener("click", function (event) {
    if (!event.target.classList.contains("remove-recipient")) return;
    if (recipients.querySelectorAll(".recipient-row").length === 1) {
      // Keep one row; just clear it.
      recipients.querySelectorAll("input, select").forEach(function (el) {
        el.value = el.tagName === "SELECT" ? "signer" : "";
      });
      return;
    }
    event.target.closest(".recipient-row").remove();
  });

  // Document source: upload vs template.
  var uploadBlock = document.getElementById("upload-block");
  var templateBlock = document.getElementById("template-block");
  var fileInput = document.getElementById("document");
  var templateSelect = document.getElementById("template_key");
  var templateHint = document.getElementById("template-hint");

  function syncSource() {
    var source = form.querySelector('input[name="doc_source"]:checked').value;
    var useTemplate = source === "template";
    uploadBlock.hidden = useTemplate;
    templateBlock.hidden = !useTemplate;
    if (useTemplate) fileInput.value = "";
  }

  form.querySelectorAll('input[name="doc_source"]').forEach(function (radio) {
    radio.addEventListener("change", syncSource);
  });

  templateSelect.addEventListener("change", function () {
    var option = templateSelect.selectedOptions[0];
    templateHint.textContent = option && option.dataset.desc ? option.dataset.desc : "";
  });

  // Client-side guard so "send" does not submit without a signer.
  form.addEventListener("submit", function (event) {
    var isSend = event.submitter && event.submitter.value === "send";
    if (!isSend) return;
    var hasSigner = Array.prototype.some.call(
      form.querySelectorAll('select[name="recipient_role"]'),
      function (select, index) {
        var email = form.querySelectorAll('input[name="recipient_email"]')[index];
        return select.value === "signer" && email && email.value.trim();
      }
    );
    if (!hasSigner) {
      event.preventDefault();
      window.App.toast("Add at least one signer with an email address.", "error");
      return;
    }
    window.App.setLoading(sendButton, true, "Sending…");
  });
})();
