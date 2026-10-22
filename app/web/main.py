"""FastAPI application. Routes stay thin; the rules live in app/services."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings
from app.db.migrations import head_revision
from app.db.session import create_db_engine
from app.logging_config import configure_logging
from app.web.middleware import log_requests
from app.web.routes import health


def create_app(settings: Settings) -> FastAPI:
    engine = create_db_engine(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        engine.dispose()

    app = FastAPI(title="Seismo", lifespan=lifespan)
    app.state.engine = engine
    app.state.expected_revision = head_revision()
    app.middleware("http")(log_requests)
    app.include_router(health.router)
    return app


def app_from_env() -> FastAPI:
    """Entry point for uvicorn (`--factory`): settings and logging come from the environment."""
    settings = Settings.from_env()
    configure_logging("api", settings.log_level_number)
    return create_app(settings)
