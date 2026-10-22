---
name: listen-ui
description: Build or change list views - tables or lists of items with sorting, one checkbox per row, bulk delete, bulk export, delete confirmation with countdown, and inline rename without page reload. Use this whenever the user mentions a Liste, Tabelle, Übersicht, Verwaltungsseite, Mehrfachauswahl, Bulk-Aktion, Massenlöschen, Export ausgewählter Einträge, Umbenennen, Löschen-Dialog, or a list/table/overview page, even if they don't say "list view".
when_to_use: "Examples: 'Baue eine Übersicht der Kategorien', 'Tags sollen mehrfach löschbar sein', 'add a table of uploaded files', 'Einträge umbenennen ohne Reload'."
---

Announce once when this skill loads: `Skill aktiv: listen-ui`.

Rules: `requirements.md`, sections "Basic UI Requirements", "Destructive and Bulk Actions", "Lists and Ordering". This skill is the implementation procedure.

## Before building

- Look for existing shared components (list/table, row checkbox, confirm dialog, inline edit) and reuse them. All pages must use identical UI elements; a second, slightly different implementation is a defect.
- If a needed component does not exist yet, build it as a shared component, not inline in this page.
- UI labels in German, using the terms from `datamodel.md`.

## Sorting

- Sort by display name, case-insensitively, in the query rather than only in the template.
- German names contain umlauts: use a collation that sorts them correctly (PostgreSQL ICU collation `de`, MariaDB `utf8mb4_unicode_ci` or newer, MongoDB `collation({locale: "de", strength: 2})`). Plain `lower()` sorts "Äpfel" after "Zitrone".

## Selection and bulk actions

- One checkbox per row plus a "select all" checkbox in the header.
- Bulk buttons (Löschen, Exportieren, ...) act on the selected rows and are disabled while nothing is selected.
- No per-row action buttons for actions that the bulk buttons cover.

## Atomic bulk actions

- Implement in the service layer, inside one transaction.
- Check all selected items for blockers first (e.g. still referenced). If any is blocked, process none and return the display names of the blocking items; show them in the message.
- A single-item delete uses the same path with one selected item.

## Delete confirmation

- The dialog states what will be deleted (count and names, truncated for long selections).
- The confirm button stays disabled for a few seconds with a visible countdown. Use one shared constant for the duration (default 3 seconds). Cancel is always available.

## Inline rename

- Save via an in-place request, no page reload.
- Success: brief "Gespeichert" next to the field. Error: inline message, keep the user's input.
- Validate on the server (empty, too long, duplicate name where names must be unique).
- Do not re-sort while the user is editing; the new position applies on the next full load.

## Full reloads

Only for actions that materially change the page (e.g. after a bulk delete), never for small edits.

## Responsive

The list must stay usable on desktop, tablet, and mobile (scroll inside its own container or switch to a card layout; decided once in the shared component).

## Tests

- Service: bulk delete with one blocked item deletes nothing and names the blocker.
- Repository: sort order with mixed case and umlauts (e.g. "birne", "Apfel", "Äpfel", "Zitrone").
- Server-side validation of rename.
- Browser behavior (selection, disabled buttons, countdown, inline save without reload): skill `verify`.
