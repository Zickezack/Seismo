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
| `worker`  | Background worker (job queue follows in a later story)                 | heartbeat file          |

- Everything except `/health/*`, `/login` and `/static/*` requires the admin login. A browser is
  sent to the login page and back afterwards; other clients get `401`. FastAPI's `/docs` and
  `/openapi.json` are switched off.
- `GET /health/live`: the api process answers.
- `GET /health/ready`: the database answers and its schema is at the newest migration (503 otherwise).
- Logs are JSON lines on stdout: `docker compose logs -f api worker`.
- Stop: `docker compose down`. The data stays in the `db-data` volume; `docker compose down -v` deletes it.

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
- A changed dependency in `pyproject.toml` needs a new image. From a terminal on the host, in the
  repository folder:
  `docker compose -f docker-compose.yml -f .devcontainer/docker-compose.dev.yml up -d --build migrate api worker`
- Logs: Docker Desktop, or `docker compose -p seismo-dev logs -f api worker` on the host.

Inside the dev container (the database is reachable as host `db`):

```sh
python -m pytest                     # all tests; integration tests use the database <POSTGRES_DB>_test
python -m pytest tests/unit          # without database
python -m alembic upgrade head       # migrate the development database
```

Browser checks (skill `verify`) use Playwright with headless Chromium; every dev container build
installs it (`python -m playwright install --with-deps chromium`).

New migration: `python -m alembic revision --autogenerate -m "<what changes>"`, then read and
correct the generated file (procedure: skill `datenmodell-aendern`).
