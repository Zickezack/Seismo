// Shared behaviour for every page, attached through data attributes: one implementation per
// behaviour, so every page acts the same (requirements.md, "Basic UI Requirements / Styleguide").
"use strict";

// Timestamps in the viewer's local time and time zone; the server sends UTC, as stored, and a page
// without JavaScript keeps showing it. The tooltip keeps the UTC value.
const localTime = new Intl.DateTimeFormat("de-CH", { dateStyle: "medium", timeStyle: "medium" });
for (const element of document.querySelectorAll("time[data-local-time]")) {
  element.title = element.textContent;
  element.textContent = localTime.format(new Date(element.dateTime));
}

// Lists with bulk actions (skill listen-ui): a checkbox per row, "select all" in the header, bulk
// buttons disabled while nothing is selected. Rows whose checkbox is disabled cannot be selected.
for (const form of document.querySelectorAll("[data-bulk-form]")) {
  const selectAll = form.querySelector("[data-select-all]");
  const selectable = () => [...form.querySelectorAll("[data-select-row]:not(:disabled)")];

  const update = () => {
    const rows = selectable();
    const selected = rows.filter((box) => box.checked).length;
    for (const button of form.querySelectorAll("[data-bulk-action]")) {
      button.disabled = selected === 0;
    }
    if (selectAll) {
      selectAll.disabled = rows.length === 0;
      selectAll.checked = selected > 0 && selected === rows.length;
      selectAll.indeterminate = selected > 0 && selected < rows.length;
    }
  };

  selectAll?.addEventListener("change", () => {
    for (const box of selectable()) {
      box.checked = selectAll.checked;
    }
    update();
  });
  form.addEventListener("change", (event) => {
    if (event.target.matches("[data-select-row]")) {
      update();
    }
  });
  update();
}

// Small edits saved in place, without a page reload, with a brief confirmation next to the button
// (requirements.md, "Destructive and Bulk Actions", item 3). On an error the input stays as typed.
for (const form of document.querySelectorAll("[data-inline-save]")) {
  const status = form.querySelector("[data-save-status]");

  const show = (text, ok) => {
    status.textContent = text;
    status.classList.toggle("save-status-ok", ok);
    status.classList.toggle("save-status-error", !ok);
  };

  form.addEventListener("input", () => show("", true));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    let text = "Speichern fehlgeschlagen.";
    let ok = false;
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: { Accept: "application/json" },
      });
      // An invalid entry answers 200 with {error}; an expired login answers 401 with {detail}.
      const body = await response.json();
      ok = response.ok && body.error === undefined;
      text = body.status ?? body.error ?? body.detail ?? text;
    } catch {
      // Network error or no JSON: the generic message above.
    }
    show(text, ok);
  });
}
