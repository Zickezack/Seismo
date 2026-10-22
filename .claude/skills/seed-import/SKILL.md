---
name: seed-import
description: Implement or extend importing seed data - demo content, starter catalog, default categories or tags, default accounts - through an explicit admin action with an idempotent seed log. Use this whenever the user mentions Seed, Seed-Import, Starterdaten, Demo-Daten, Beispieldaten, Grunddaten, Standardkatalog, initiale Daten, Vorbefüllung, or wants data to exist "von Anfang an", even if they don't say "seed".
---

Announce once when this skill loads: `Skill aktiv: seed-import`.

Rules: `architecture.md`, sections "Seed Data" and "Deployment and Persistence". This skill is the implementation procedure.

## Design

1. Trigger: one admin-only action - a button in Plattform-Einstellungen plus a POST route. Nothing else imports seed data: not startup, page load, request middleware, Alembic migrations, or deploy.
2. Seed source: versioned files in the repo (e.g. `seeds/<source>.json`). Every item carries a stable key that never changes and is independent of its display name.
3. Seed log: a table or collection with a unique key `(source, item_key)` and `imported_at`. It records "this item was imported once" - not whether the record still exists.
4. Per item: if a log entry exists, skip it, even if the record was renamed or deleted since. Otherwise insert the record and its log entry in the same transaction.
5. Concurrency guard: the unique constraint on the log key (double click, two workers). On a unique violation, roll back that item and count it as skipped. MongoDB without transactions: insert the log entry first (unique index), then the record; if the record insert fails, remove the log entry.
6. Items added to the seed source later are imported on the next explicit run; existing ones stay untouched.
7. Re-importing a deleted seed item is a separate, explicit admin action that removes that item's log entry. Never the default.
8. Result shown to the admin: number imported, number skipped.

## Tests

- Two runs in a row produce no duplicates.
- Rename a seeded item, run again: not recreated.
- Delete a seeded item, run again: not recreated.
- Two concurrent runs: each item exists exactly once.
- A non-admin cannot trigger the import.
- App start and page loads create no seed rows.
