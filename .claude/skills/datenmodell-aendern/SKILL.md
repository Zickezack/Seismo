---
name: datenmodell-aendern
description: Change the data model safely - add, rename, or remove fields, tables, collections, or entities, with Alembic migrations for SQL or additive default reads for MongoDB, and keep datamodel.md current. Use this whenever the user mentions neues Feld, Spalte, Attribut, Tabelle, Collection, Entität, Relation, Schema, Migration, Alembic, Datenmodell, Feld umbenennen, or a feature needs to store something new, even if they don't mention the database.
---

Announce once when this skill loads: `Skill aktiv: datenmodell-aendern`.

Rules: `architecture.md` (preferred stores, Alembic, additive reads, "Deployment and Persistence"), `ai-rules.md` ("Security and Privacy"), `datamodel.md` ("Conventions"). This skill is the procedure.

## 1. Classify

- Additive (new optional field, new table or collection) or breaking (rename, type change, removal, new required field without default).
- Which store: SQL or MongoDB.
- Breaking changes need the user's approval of the approach before implementation.

## 2a. SQL with Alembic

1. Change the model, generate the migration, then read and correct the generated file. Autogenerate misdetects some changes - a rename shows up as drop plus add, which destroys the column's data.
2. Write `downgrade` as well as `upgrade`.
3. New column on an existing table: nullable or with a server default. Make it required in a later step, after a backfill.
4. Rename with a rename/alter operation, never drop plus add.
5. Migrations contain schema changes and required backfills only - never seed data (skill `seed-import`).
6. Test: upgrade on a database with existing rows (not only an empty one), downgrade, upgrade again.

## 2b. MongoDB

1. New field: no migration. Read it everywhere with `doc.get("field", default)`; write it on new and updated documents.
2. Rename, type change, or removal: either read old and new name during a transition, or run an idempotent, rerunnable migration script.
3. New queries on the field: check whether an index is needed.

## 3. Both stores

- Persistence: data lives only in the database volume, never in files inside the app container.
- Privacy: mark sensitive fields `[privacy]` in `datamodel.md`; decide who may read and export them; never write them to logs.
- Only the repository layer touches the schema; services never use raw queries.
- Update `datamodel.md` using the template in its "Entities" section, including the German UI term.
- Tests: repository tests including existing rows (SQL) or old documents without the field (MongoDB).
