# One image for the migrate, api and worker services; docker-compose.yml picks the command.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall them. The empty package lets the
# editable install run before the code is copied; the real code is then picked up in place.
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py && pip install -e .

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

RUN useradd --system --uid 10001 --no-create-home app
USER app

EXPOSE 8000
CMD ["uvicorn", "app.web.main:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]
