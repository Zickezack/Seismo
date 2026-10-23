# One image for the migrate, api and worker services; docker-compose.yml picks the command.
FROM python:3.14-slim AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# pg_dump and pg_restore for the backups (README.md, "Backups"); system packages change least often.
COPY scripts/install-postgres-client.sh /tmp/
RUN /tmp/install-postgres-client.sh && rm /tmp/install-postgres-client.sh

# Dependencies first, so a code change does not reinstall them. The empty package lets the
# editable install run before the code is copied; the real code is then picked up in place.
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py && pip install -e .

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

RUN useradd --system --uid 10001 --no-create-home app
# Mount point of the worker's backup volume. A new volume takes over this owner, so the app user can write.
RUN install -d -o app -g app /backups
USER app

EXPOSE 8000
CMD ["uvicorn", "app.web.main:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]

# Dev stack only (.devcontainer/docker-compose.dev.yml): it mounts the workspace code over the copy
# above, and watchfiles restarts api and worker on every change.
FROM app AS dev
USER root
RUN pip install -e ".[reload]"
USER app

# The last stage is what a plain build produces: the production image, without dev tools.
FROM app
