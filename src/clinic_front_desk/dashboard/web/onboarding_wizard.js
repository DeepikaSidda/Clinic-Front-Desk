// Progressive-enhancement for the server-rendered OnboardingWizard (task 13.4).
//
// The form is fully functional without JavaScript. This snippet only adds
// convenience: cloning a fresh service/provider row, removing rows, and
// disabling a day's open/close inputs when it is marked "Closed". All
// server-side validation, saving, and value retention (Req 1.4-1.6) happen in
// Python; nothing here talks to the network.
(function () {
  "use strict";

  var form = document.getElementById("onboarding-wizard");
  if (!form) {
    return;
  }

  // Re-index the name="collection[i].attr" attributes of a row after add/remove
  // so the server parses a contiguous 0..n-1 sequence.
  function reindex(listId, rowClass) {
    var list = document.getElementById(listId);
    if (!list) {
      return;
    }
    var rows = list.querySelectorAll("." + rowClass);
    Array.prototype.forEach.call(rows, function (row, index) {
      row.setAttribute("data-index", String(index));
      var fields = row.querySelectorAll("[name]");
      Array.prototype.forEach.call(fields, function (field) {
        var name = field.getAttribute("name");
        field.setAttribute("name", name.replace(/\[\d+\]/, "[" + index + "]"));
      });
    });
  }

  function addRow(listId, rowClass, reindexTarget) {
    var list = document.getElementById(listId);
    if (!list) {
      return;
    }
    var rows = list.querySelectorAll("." + rowClass);
    if (rows.length === 0) {
      return;
    }
    var clone = rows[rows.length - 1].cloneNode(true);
    var inputs = clone.querySelectorAll("input, textarea");
    Array.prototype.forEach.call(inputs, function (input) {
      if (input.type === "checkbox") {
        input.checked = false;
      } else {
        input.value = "";
      }
    });
    // Drop any per-field error annotations carried over by the clone.
    var errors = clone.querySelectorAll(".field-error");
    Array.prototype.forEach.call(errors, function (node) {
      node.parentNode.removeChild(node);
    });
    list.appendChild(clone);
    reindex(listId, reindexTarget);
  }

  form.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || !target.getAttribute) {
      return;
    }
    var action = target.getAttribute("data-action");
    if (action === "add-service") {
      addRow("services-list", "service-row", "service-row");
    } else if (action === "remove-service") {
      var serviceRow = target.closest(".service-row");
      if (serviceRow) {
        serviceRow.parentNode.removeChild(serviceRow);
        reindex("services-list", "service-row");
      }
    } else if (action === "add-provider") {
      addRow("providers-list", "provider-row", "provider-row");
    } else if (action === "remove-provider") {
      var providerRow = target.closest(".provider-row");
      if (providerRow) {
        providerRow.parentNode.removeChild(providerRow);
        reindex("providers-list", "provider-row");
      }
    }
  });

  // Disable a weekday's time inputs while it is marked closed.
  form.addEventListener("change", function (event) {
    var target = event.target;
    if (!target || target.type !== "checkbox") {
      return;
    }
    var name = target.getAttribute("name") || "";
    if (name.indexOf(".closed") === -1) {
      return;
    }
    var cell = target.closest("tr");
    if (!cell) {
      return;
    }
    var times = cell.querySelectorAll('input[type="time"]');
    Array.prototype.forEach.call(times, function (input) {
      input.disabled = target.checked;
    });
  });
})();
