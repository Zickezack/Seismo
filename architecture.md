# Architecture

This document describes the planned structure of the system and the high-level components.

## Main purpose

Self-hosted platform that turns global news into a persistent, versioned event and knowledge model and derives verifiable hypotheses about economic developments and financial markets. It supports research; it does not trade autonomously. Goals, stages and acceptance criteria: [`requirements.md`](requirements.md); epics and priorities: [`userstories.md`](userstories.md); domain terms and data rules: [`datamodel.md`](datamodel.md).

## Architecture principles

1. **Local-first:** ingestion, processing, storage and inference run locally wherever possible. External data sources are connected through defined interfaces.
2. **Event-centric:** the durable main object is the event (Ereignis), not the individual article.
3. **Evidence-first:** every material statement must be traceable to its sources, timestamps and extraction versions.
4. **Temporal versioning:** events, relationships and hypotheses change over time. Earlier states must never be overwritten unnoticed.
5. **Incremental processing:** new articles update existing events or create new ones. The full history is not re-analysed on every run.
6. **Raw data with a lifecycle:** full texts are kept only as long as they are needed and the data rights allow it. Metadata and permissible source references are kept.
7. **Modular scaling:** sources, database, workers, embedding model and LLM can be replaced or scaled independently.
8. **Reproducible evaluation:** forecasts are logged before the outcome is known and later compared with actual market movements.

## Structure

- Devcontainers are to be used for quick development.
- I need Claude Code in the devcontainer for development. So after rebuilding it shall also be available.
- Micro Services used where applicable i.e. Docker Compose structure.
- Preferred services (only use if applicable): Jinja for templates, FastAPI for API, PostgreSQL or MariaDB for SQL (or SQLite for lightweight/temporary) with Alembic for migrations, MongoDB for JSON like DB, otherwise use the most commonly used applicable services.
- Stack: Python; FastAPI with Jinja templates for API and dashboard (the frontend technology is re-evaluated after the first end-to-end prototype); PostgreSQL with SQLAlchemy and Alembic; a separate Python worker with a PostgreSQL-based job queue; Ollama for LLM inference, outside the Compose stack (one instance per environment, see "Deployment and Persistence").
- Added only once measurements show a need: Redis or a specialised queue instead of the PostgreSQL job queue, pgvector for semantic search, a graph database for the knowledge model, and a larger LLM or a more powerful GPU (only when inference quality or throughput is the limiting factor). Semantic clustering is used only where simple methods are not sufficient.
- Observability: structured logs, health checks, runtime metrics and job status.
- At the beginning, the system should be a monorepo, but later on, it can be split into multiple repos if necessary.
- We use only admin login defined in env file at first, but later on, we can implement a user management system with roles and permissions (see "Optional feature backlog" below for what that tends to involve).
- For a small, schema-less datastore (e.g. a document database), prefer additive, backward-compatible reads (`value = document.get("key", default)`) over a formal migration step whenever a new field is introduced - reserve real migrations for changes that can't be expressed that way. Procedure for any data model change: skill `datenmodell-aendern`.
- Keep a short list of "law of the land" rules that nothing else is allowed to override (e.g. a native `hidden` attribute must always mean "not displayed", full stop). A later, more specific style rule silently winning over a more fundamental one is a recurring, hard-to-diagnose class of bug.

## Deployment and Persistence

- I want a deployment pipeline for Portainer. This can also be done at a later point, but necessary for live deployment! Only the main branch should be deployed to the live server, and optionally, a dev branch can be deployed to a staging server.
- Environments and LLM: development runs on the developer's machine against an Ollama on its localhost (reached from inside a container as `host.docker.internal`); live runs in the local LAN against the dedicated Ollama server there. The Compose stack never contains Ollama, and its URL comes only from the environment (`OLLAMA_BASE_URL`), never from code or the image.
- Run the test suite in CI before an image is built and pushed, so a broken build never reaches the deployment step.
- Be explicit about which storage is actually persistent across a redeploy (typically only a database's own data volume) versus what is rebuilt from the image every time (the application container's own filesystem, including anything written to it at runtime). Anything meant to survive a redeploy must live in the persistent store, not be written to disk inside the app container.
  Persistent here: the database volume `db-data`, and the worker's backup store `/backups` - the volume `db-backups`, or with `BACKUP_HOST_DIR` a host directory off the Docker host's disk (README.md, "Backups").
- Setup procedure: skill `deploy-setup`.

## Stages and hardware sizing

The platform grows in four stages; goals and success criteria per stage are in `requirements.md`, "Stages". The resources below were proposed by an AI. They serve the user's hardware planning: they must not drive or limit the functionality of a stage, and they are shown and re-evaluated whenever a stage is implemented.

| Stage | Resources |
|-------|-----------|
| 1 - Functional prototype | Existing AI-Station with GTX 1080 (8 GB VRAM) and 16 GB RAM; 100-250 GB free storage as starting budget; local LLM via Ollama; PostgreSQL as the target for the data model, SQLite only for an isolated short test. |
| 2 - Continuous operation | 32-64 GB RAM; 1-2 TB NVMe SSD; keep the existing GPU at first; separate worker process and monitoring. |
| 3 - Historical research | 64-128 GB RAM; 2-4 TB NVMe or a separate archive; optionally a GPU with 16-24 GB VRAM if measurements show a need; historical market, company and sector data. |
| 4 - Scaled analysis | 128 GB RAM or more after load measurement; 4-8 TB or more NVMe/archive storage depending on data volume; a GPU with 24-48 GB VRAM or several GPUs if inference throughput requires it; several workers, a job queue, optionally object storage or an analytical column store. |

- After the first sprint, real measurements decide whether more RAM, more storage, a faster GPU or better source coverage is needed first.
- A larger model or a more powerful GPU becomes a goal only when measurements show that inference quality or throughput is actually the limiting factor.

## Seed Data

- Seed data (demo content, a starter catalog, default accounts) is imported only through an explicit, admin-triggered action - never implicitly on a page load, per request, on startup, in a migration, or on deploy. Implicit "seed if not already present" logic races: concurrent requests (two users, two warm workers, a deploy racing a health check) each see "not seeded yet" and each insert the full seed set.
- "Already seeded" is tracked in a seed log keyed by the seed source's own stable identifier, independent of the seeded records' mutable state - not by whether a record with the original name still exists. Otherwise renaming or deleting a seeded item makes the importer recreate it on the next run.
- Procedure: skill `seed-import`.

## Codebase structure

Conventions for where new code goes. Procedure for implementing a feature: skill `neues-feature`.

- Keep data access (repositories) and business rules (services) in separate layers even inside a small monorepo, with thin route/controller handlers - this keeps the logic testable without spinning up the web framework.
- Pick one convention for request-time checks (manual auth/permission checks at the top of each handler, or a dependency-injection mechanism) and apply it uniformly across all routes, rather than mixing both styles.
  Chosen: dependency injection. `app/web/main.py` includes every router with `Depends(require_admin)` (`app/web/access.py`), except the public ones it lists explicitly (`/health/*`, `/login`, `/logout`, `/static`); `tests/unit/test_web_auth.py` fails for any other route that answers without a session.
- Services own their transaction and take an `Engine`. A function meant to run inside its caller's transaction takes a `Connection` instead (e.g. `job_service.enqueue` inside a job's effect).
- Background work runs as jobs in the PostgreSQL queue (`app/jobs/`). Handlers are two-phase: `handler(job) -> effect | None`. Slow work (an LLM call, a fetch) happens outside any transaction; the returned effect writes the result in the transaction that marks the job done, after the lease check, so a retried job has no duplicate effect. A new job kind: a handler in `app/jobs/handlers.py` and its German label in `job_service.KIND_LABELS`.
- Source adapters live in `app/sources/`: each implements `SourceAdapter.fetch` (`base.py`) and returns `FetchedItem`s; HTTP only through `HttpClient` (`http.py`: timeout, size limit, User-Agent with `FETCH_CONTACT`). A new source type: an adapter module and its entry in `ADAPTERS` (`app/sources/__init__.py`); the source form offers it from there. `SourceRejected` fails the fetch job at once, `SourceUnavailable` retries it. The worker queues due fetches once a minute (`ingestion_service.enqueue_due_fetches`).
- An `ON CONFLICT` on a partial unique index writes the index predicate's values into the SQL (`literal(..., literal_execute=True)`), never as bound parameters: from the 11th run on one connection PostgreSQL plans the prepared statement generically and no longer matches the index (`job_repository.insert_job`).
- Every LLM call runs inside `LlmCallLimiter.slot()` (`app/llm/limiter.py`), which caps parallel calls across all workers with PostgreSQL advisory locks (Plattform-Einstellung).
- Shared browser behaviour lives in `app/web/static/app.js`, attached through data attributes (`data-bulk-form`, `data-inline-save`, `data-local-time`, `data-confirm-delete`), never as a page's own script. Every delete goes through the one confirmation dialog in `templates/base.html` (`data-confirm-delete`). An all-or-nothing bulk action returns `BulkResult` (`app/services/bulk.py`). A message after a redirect goes through `flash()` (`app/web/templating.py`). An in-place save answers an invalid entry with 200 and `{"error": ...}`, never with 4xx: browsers log every 4xx answer as a console error.
- Timestamps are stored in UTC and shown in the viewer's local time (template filter `local_time`; without JavaScript they stay in UTC).
- Directory layout: one Python package and one Docker image. Compose runs that image as the `api` and the `worker` service with different commands, so both share the domain code and still scale independently.

  ```text
  .devcontainer/      dev container, Claude Code included
  docker-compose.yml  services api, worker, db
  Dockerfile          one image for api and worker
  .env.example        variable names and placeholders only
  pyproject.toml
  scripts/            install-postgres-client.sh: pg_dump/pg_restore for image and dev container
  app/
    config.py         settings from environment variables
    db/               SQLAlchemy models and session
    repositories/     data access only
    services/         business rules, no web framework objects
    sources/          source adapters (RSS, GDELT, ...)
    llm/              Ollama client, event schema
    jobs/             PostgreSQL job queue
    worker/           worker entry point
    backup/           backup command line: python -m app.backup list | create | restore
    seeds/            seed files, e.g. sources.json (starter catalogue of Quellen); imported only by the admin action ("Seed Data")
    web/              thin FastAPI routes, Jinja templates, static files
  migrations/         Alembic
  tests/
    unit/
    integration/
  ```

## Optional feature backlog

Not every platform needs these - add an item only once the project actually calls for it, scoped to what's needed rather than building the general case up front:

- **User management with roles and permissions.** Start with a single admin login from an env var; add multi-user accounts, roles, and per-resource sharing/permissions once more than one person actually needs to use the system.
- **Email-based flows** (invite links, password reset) once there is more than one user - needs an outgoing-mail setup (even a local catch-all mailbox for development) and a way to point at a real relay per deployment without a code change.
- **A dedicated admin settings area** once there is enough admin-only configuration to justify one - split into Plattform-Einstellungen and Benutzer-Einstellungen (`requirements.md`, "Settings") and apply the navigation/layout guidance from `requirements.md` as soon as an area grows past a single section. Procedure: skill `settings-seite`.
- **An editable library/catalog of shared, reusable assets** (tags, categories, uploaded media) that records attach to by reference, administered in one place rather than duplicated per record.
