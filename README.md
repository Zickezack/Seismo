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

All configuration comes from environment variables. Copy the template once and set a password:

```sh
cp .env.example .env
```

`.env` is gitignored. On Portainer, the same variables are set in the stack environment.

## Run the stack

```sh
docker compose up -d --build
docker compose ps
```

| Service   | Role                                                                   | Health check            |
|-----------|------------------------------------------------------------------------|-------------------------|
| `db`      | PostgreSQL 18; volume `db-data` is the only persistent storage         | `pg_isready`            |
| `migrate` | Runs `alembic upgrade head` once, then exits; `api` and `worker` wait for it | exit code 0       |
| `api`     | FastAPI on `http://localhost:${API_PORT:-8000}`                        | `GET /health/ready`     |
| `worker`  | Background worker (job queue follows in a later story)                 | heartbeat file          |

- `GET /health/live`: the api process answers.
- `GET /health/ready`: the database answers and its schema is at the newest migration (503 otherwise).
- Logs are JSON lines on stdout: `docker compose logs -f api worker`.
- Stop: `docker compose down`. The data stays in the `db-data` volume; `docker compose down -v` deletes it.

## Develop in the dev container

Requirements: Docker and VS Code with the Dev Containers extension.

1. Create `.env` (see above).
2. Run **Dev Containers: Reopen in Container**. It starts the `dev` container and `db`.
3. Claude Code is installed on every build. Its login and settings live in the `claude-config`
   volume, so a rebuild keeps them. Sign in once with `claude`.

Inside the dev container (the database is reachable as host `db`):

```sh
python -m pytest                     # all tests; integration tests use the database <POSTGRES_DB>_test
python -m pytest tests/unit          # without database
python -m alembic upgrade head       # migrate the development database
python -m uvicorn app.web.main:app_from_env --factory --reload --host 0.0.0.0   # api on port 8000
python -m app.worker                 # worker
```

New migration: `python -m alembic revision --autogenerate -m "<what changes>"`, then read and
correct the generated file (procedure: skill `datenmodell-aendern`).
