# Seismo

Self-hosted platform that turns global news into a persistent, versioned event and knowledge model.
Goals and rules: [`requirements.md`](requirements.md), [`architecture.md`](architecture.md),
[`datamodel.md`](datamodel.md), [`userstories.md`](userstories.md). AI contributors start with
[`ai-rules.md`](ai-rules.md).

## Repository

https://github.com/Zickezack/Seismo, branch `main`:

```sh
git clone https://github.com/Zickezack/Seismo.git
```

`.gitignore` keeps `.env`, key files and local settings (`.claude/settings.local.json`,
`CLAUDE.local.md`, `docker-compose.override.yml`, editor folders) out of the repository.
`.gitattributes` keeps LF line endings in every checkout, also on Windows.

## Configuration

All configuration comes from environment variables. Copy the template once and replace the
placeholders:

```sh
cp .env.example .env
```

`.env` is gitignored. On Portainer, the same variables are set in the stack environment.

The dashboard needs an admin login; the api refuses to start without it:

| Variable | Rule |
|----------|------|
| `ADMIN_USERNAME` | Login name of the admin. |
| `ADMIN_PASSWORD` | At least 12 characters. |
| `SESSION_SECRET` | At least 32 characters, e.g. `openssl rand -hex 32`. |
| `SESSION_COOKIE_SECURE` | Optional, default `false`; `true` only once the api is served over HTTPS. |

Only the `api` container gets these variables. Changing `ADMIN_PASSWORD` or `SESSION_SECRET` logs
out every open session; a session otherwise lasts 12 hours from the login.

`FETCH_CONTACT` (optional, strongly recommended) is an e-mail address or URL sent in the User-Agent of
every source fetch, so a source can reach the operator; some sources ask for it - www.admin.ch (BFS)
answers full-text requests without one with HTTP 403, and the SEC and the BLS want an e-mail address (a URL
gets HTTP 403 there). The worker sends it; the api only shows a
warning on the source list while it is missing. After setting it, recreate both containers
(`docker compose up -d api worker`), then select the affected sources and use "Blockierte Volltexte abrufen".

`RELIEFWEB_APPNAME` (optional) is the appname a Quelle of type ReliefWeb sends with every API request. ReliefWeb
answers only an appname it has approved: request one with the form linked at https://apidoc.reliefweb.int/#appname
(organisation, purpose and random characters, e.g. `ost-seismo-k7q2`); ReliefWeb replies by e-mail. Only the
worker sends it; after setting it, recreate the worker (`docker compose up -d worker`).

`EIA_API_KEY` (optional) is the key a Quelle of type EIA sends to the EIA Open Data API. Register for one
at https://www.eia.gov/opendata/register.php (name and e-mail address; the key comes by e-mail at once). It is a
secret: only the worker gets it, it never goes into a Quelle's URL (the form refuses an `api_key` parameter) and
never into the log. After setting it, recreate the worker. An EIA Quelle's URL is the "API URL" that EIA's API
browser (https://www.eia.gov/opendata/browser) shows for a data set, e.g.
`https://api.eia.gov/v2/natural-gas/stor/wkly/data/?frequency=weekly&data[0]=value`.

The event extraction asks an Ollama server (worker only; empty `OLLAMA_BASE_URL`: no extraction):

| Variable | Rule |
|----------|------|
| `OLLAMA_BASE_URL` | The server, e.g. `http://192.168.1.20:11434`. Live: the LLM server in the LAN; the dev stack has its own. |
| `OLLAMA_MODEL` | Optional, default `qwen3.5:9b`; it must be pulled on that server (`ollama pull`). |
| `OLLAMA_NUM_CTX` | Optional, default `8192`, at least `6144`: the model's context in tokens; longer texts are cut to fit. |

Which articles reach the model, and how many per hour, is set in the Plattform-Einstellungen (Ereignisextraktion)
and per source (Ereignisse extrahieren).

## Run the stack

```sh
docker compose up -d --build
docker compose ps
```

| Service   | Role                                                                   | Health check            |
|-----------|------------------------------------------------------------------------|-------------------------|
| `db`      | PostgreSQL 18; volume `db-data` is the only persistent storage         | `pg_isready`            |
| `migrate` | Runs `alembic upgrade head` once, then exits; `api` and `worker` wait for it | exit code 0       |
| `api`     | FastAPI and dashboard on `http://localhost:${API_PORT:-8000}`          | `GET /health/ready`     |
| `worker`  | Runs the jobs of the job queue, including the backups                  | heartbeat file          |

- Everything except `/health/*`, `/login` and `/static/*` requires the admin login. A browser is
  sent to the login page and back afterwards; other clients get `401`. FastAPI's `/docs` and
  `/openapi.json` are switched off.
- `GET /health/live`: the api process answers.
- `GET /health/ready`: the database answers and its schema is at the newest migration (503 otherwise).
- Logs are JSON lines on stdout: `docker compose logs -f api worker`.
- Stop: `docker compose down`. The data stays in the `db-data` volume, the backups in `db-backups`;
  `docker compose down -v` deletes both.
- A stopping `worker` finishes its current job first. While an event extraction runs, a stop or a redeploy
  waits for it - at most 6 minutes (`stop_grace_period`, longer than the 5 minutes an LLM call may take).

## Backups

The worker backs up the database on its own, as a job `Datenbank-Backup` of the job queue: status,
attempts and the last error show on the **Jobs** page. A backup is due once the newest backup file is
older than the backup interval; after each new backup the oldest files beyond the number to keep are
deleted. Both are set under **Einstellungen -> Backups** (default: every 24 hours, keep 14).

- File: `<POSTGRES_DB>-<UTC time>.dump`, e.g. `seismo-20261023T030000Z.dump`, compressed `pg_dump`
  format. A dump that broke off never gets this name.
- Uploaded: `<POSTGRES_DB>-upload-<UTC time>.dump`, a backup from elsewhere waiting for a restore. It
  counts neither for the schedule nor for the number to keep, and stays until it is deleted.
- Place: `/backups` in the worker and the api container (the api lists, serves and takes uploads). By default that is the Docker volume `db-backups` on the
  same disk as the database - it covers mistakes and broken migrations, not a lost disk. For that, set
  `BACKUP_HOST_DIR` to an absolute host path, e.g. a mounted NAS share, writable for user id 10001:
  `sudo install -d -o 10001 /mnt/nas/seismo-backups`.
- `pg_dump` and `pg_restore` come from the PostgreSQL apt repository
  ([`scripts/install-postgres-client.sh`](scripts/install-postgres-client.sh)), in the image and in the
  dev container. Their major version must equal the `db` image's; a test checks it. Raise both together.

Commands, in the worker container (on the host: `docker compose exec worker <command>`; on Portainer:
the worker container's console):

```sh
python -m app.backup list      # the backups, newest first
python -m app.backup create    # a backup now, e.g. before a risky change
```

The same without a console, under **Einstellungen -> Backup-Dateien**: every backup with download, upload of
a backup file (only `pg_dump`'s custom format, as Seismo writes it), deleting selected ones, and "Backup jetzt
erstellen", which queues the job. A restore cannot run from there - it needs api and worker stopped.

### Configuration export and import

**Einstellungen -> Import und Export** writes the Plattform-Einstellungen and all Quellen into one JSON file,
to set up a new instance; the source list exports selected Quellen (bulk action "Exportieren"). A Quelle carries
the fields of its form only - no fetch status, no articles, and never an access key, which stays in the
environment (`RELIEFWEB_APPNAME`, `EIA_API_KEY`). The import takes the Einstellungen, the Quellen or both: the
whole file is checked first and an invalid one changes nothing; a Quelle whose name exists is skipped, never
overwritten. Articles and events go with the backups.

### Restore

A restore loads the backup into a database of its own first and replaces the current database only
once the backup has loaded completely. The current database is never deleted: it stays as
`<POSTGRES_DB>_before_restore_<time>` until you drop it.

With Docker Compose, on the host:

```sh
docker compose stop api worker
docker compose run --rm --no-deps worker python -m app.backup restore seismo-20261023T030000Z.dump
docker compose up -d     # migrate brings a backup of an older version to the newest schema
```

On Portainer: stop the `api` container, open the console of the `worker` container, run
`python -m app.backup restore <file>` and then `alembic upgrade head`, restart `worker`, start `api`.
The restore ends the worker's open database sessions itself.

The command asks for the database name as confirmation (`--yes` skips that) and prints the name the
replaced database got. Once the restored data has been checked, drop the replaced database:

```sh
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE \"seismo_before_restore_20261023093000\""'
```

### Restore tests

| Date | Where | What | Result |
|------|-------|------|--------|
| 2026-10-23 | Dev container (Debian 13, `pg_dump`/`pg_restore` 18.6) against the dev stack's PostgreSQL 18.6; disposable database `seismo_restoretest` | 10'000 jobs and changed Plattform-Einstellungen. `create` (0.1 MB, 1 s), then half the jobs deleted and a setting changed; `restore` without confirmation, then with it (1 s). A second backup at the previous schema version `724a65db6143`, restored and migrated with `alembic upgrade head`. | Without confirmation nothing changed. After the restore 10'000 jobs with the same checksum as at backup time and the old setting; the replaced database kept with the later state. The older backup reached the newest schema and the readiness check passed. Not yet run in the Docker image on the AI-Station - due after the first deployment. |

## Develop in the dev container

Requirements: Docker and VS Code with the Dev Containers extension.

1. Create `.env` (see above). The dev container needs the admin variables as well.
2. Run **Dev Containers: Reopen in Container**. It starts the `dev` container and, next to it,
   `db`, `migrate`, `api` and `worker` (Compose project `seismo-dev`).
3. Claude Code is installed on every build. Its login and settings live in the `claude-config`
   volume, so a rebuild keeps them. Sign in once with `claude`.

The dashboard of the dev stack is at `http://localhost:${API_PORT:-8000}`. The stack from
`docker compose up` uses the same port, so run only one of the two at a time.

`migrate`, `api` and `worker` run the workspace code: `app/` and `migrations/` are mounted into the
image `seismo:dev` (Dockerfile target `dev`).

- A saved `.py` file restarts `api` and `worker` within about a second; a changed template or
  stylesheet applies at once. Reload the page in the browser to see it.
- A new migration: `python -m alembic upgrade head` in the dev container (`migrate` only runs when
  the stack starts).
- A changed dependency in `pyproject.toml` needs a new image. **Dev Containers: Rebuild Container**
  builds every service of `runServices` anew - `migrate`, `api` and `worker` included - and restarts
  them; plain *Reopen in Container* builds nothing. The rebuild takes a few minutes and ends a Claude
  Code session in the container (login and history stay in the `claude-config` volume). Faster, and
  without touching the dev container, from a terminal on the host in the repository folder:
  `docker compose -f docker-compose.yml -f .devcontainer/docker-compose.dev.yml up -d --build migrate api worker`
- Logs: Docker Desktop, or `docker compose -p seismo-dev logs -f api worker` on the host.

The dev stack has an Ollama of its own for the event extraction, on the host's NVIDIA GPU (Docker
Desktop with GPU support in WSL2). It is not started with the dev container; once, from a terminal on the
host in the repository folder:

```sh
docker compose -f docker-compose.yml -f .devcontainer/docker-compose.dev.yml up -d ollama
docker compose -f docker-compose.yml -f .devcontainer/docker-compose.dev.yml exec ollama ollama pull qwen3.5:9b
```

The models stay in the volume `ollama-models`. `api`, `worker` and the dev container reach it as
`http://ollama:11434`. A larger model for development: pull it and set `OLLAMA_MODEL` in `.env`.

Inside the dev container (the database is reachable as host `db`):

```sh
python -m pytest                     # all tests; integration tests use the database <POSTGRES_DB>_test
python -m pytest tests/unit          # without database
python -m alembic upgrade head       # migrate the development database
```

Browser checks (skill `verify`) use Playwright with headless Chromium; every dev container build
installs it (`python -m playwright install --with-deps chromium`). Every build also installs `pg_dump`
and `pg_restore` for the backup tests (`scripts/install-postgres-client.sh`); without them those tests fail.
The `worker` of the dev stack makes backups only once its image is rebuilt (command above).

New migration: `python -m alembic revision --autogenerate -m "<what changes>"`, then read and
correct the generated file (procedure: skill `datenmodell-aendern`).
