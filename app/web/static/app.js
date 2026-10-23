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

// In-page navigation of a page with several sections (requirements.md, "Basic UI Requirements /
// Styleguide", item 4): a link expands its section if it is collapsed; the browser then scrolls to it.
for (const nav of document.querySelectorAll("[data-section-nav]")) {
  nav.addEventListener("click", (event) => {
    const link = event.target.closest("a[href^='#']");
    const section = link && document.getElementById(link.hash.slice(1));
    if (section instanceof HTMLDetailsElement) {
      section.open = true;
    }
  });
}

// Delete confirmation (requirements.md, "Destructive and Bulk Actions", item 1): a delete button of a
// bulk form opens the dialog of templates/base.html, which names what goes and unlocks its confirm
// button only after a short countdown. data-confirm-delete holds the item word, "singular|plural".
const CONFIRM_DELAY_SECONDS = 3;
const MAX_NAMES_IN_DIALOG = 10;
const confirmDialog = document.querySelector("[data-confirm-dialog]");
if (confirmDialog) {
  const title = confirmDialog.querySelector("[data-confirm-title]");
  const nameList = confirmDialog.querySelector("[data-confirm-names]");
  const confirmButton = confirmDialog.querySelector("[data-confirm-button]");
  let pendingButton = null;
  let timer = null;

  const countDown = (seconds) => {
    confirmButton.disabled = seconds > 0;
    confirmButton.textContent = seconds > 0 ? `Löschen (${seconds})` : "Löschen";
    if (seconds > 0) {
      timer = setTimeout(() => countDown(seconds - 1), 1000);
    }
  };

  for (const button of document.querySelectorAll("[data-confirm-delete]")) {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      const [singular, plural] = button.dataset.confirmDelete.split("|");
      const names = [...button.form.querySelectorAll("[data-select-row]:checked")].map((box) => box.dataset.name);
      title.textContent = `${names.length} ${names.length === 1 ? singular : plural} löschen?`;
      const shown = names.slice(0, MAX_NAMES_IN_DIALOG);
      if (names.length > shown.length) {
        shown.push(`und ${names.length - shown.length} weitere`);
      }
      nameList.replaceChildren(...shown.map((name) => Object.assign(document.createElement("li"), { textContent: name })));
      pendingButton = button;
      // Escape keeps the previous return value; a stale "confirm" must never delete.
      confirmDialog.returnValue = "";
      confirmDialog.showModal();
      countDown(CONFIRM_DELAY_SECONDS);
    });
  }

  confirmDialog.addEventListener("close", () => {
    clearTimeout(timer);
    if (confirmDialog.returnValue === "confirm" && pendingButton) {
      // Submits with the button's formaction; no click event, so this handler does not run again.
      pendingButton.form.requestSubmit(pendingButton);
    }
    pendingButton = null;
  });
}
