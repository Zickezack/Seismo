"""FastAPI application. Routes stay thin; the rules live in app/services."""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import AuthSettings, Settings
from app.db.migrations import head_revision
from app.db.session import create_db_engine
from app.logging_config import configure_logging
from app.services.auth_service import session_signing_key
from app.web.access import SESSION_COOKIE, SESSION_MAX_AGE_SECONDS, NotAuthenticated, redirect_to_login, require_admin
from app.web.middleware import log_requests
from app.web.routes import articles, auth, dashboard, events, health, jobs, sources
from app.web.routes import settings as platform_settings
from app.web.templating import STATIC_DIR


def create_app(settings: Settings, auth_settings: AuthSettings) -> FastAPI:
    engine = create_db_engine(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        engine.dispose()

    # No /docs and /openapi.json: they would list every route to anyone without a login.
    app = FastAPI(title="Seismo", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.expected_revision = head_revision()
    app.state.auth = auth_settings
    # Only shown: whether the worker can name its operator in the User-Agent (routes/sources.py).
    app.state.fetch_contact = settings.fetch_contact
    # Only shown: which LLM server the worker extracts with, and whether it serves the model (routes/settings.py).
    app.state.llm = (settings.ollama_base_url, settings.ollama_model, settings.ollama_num_ctx)
    app.middleware("http")(log_requests)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_signing_key(auth_settings),
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE_SECONDS,
        same_site="lax",
        https_only=auth_settings.session_cookie_secure,
    )
    app.add_exception_handler(NotAuthenticated, redirect_to_login)

    # Public: health checks for Docker, the login page and its stylesheet. Every other router
    # is included with require_admin (access convention, app/web/access.py).
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(dashboard.router, dependencies=[Depends(require_admin)])
    app.include_router(sources.router, dependencies=[Depends(require_admin)])
    app.include_router(articles.router, dependencies=[Depends(require_admin)])
    app.include_router(events.router, dependencies=[Depends(require_admin)])
    app.include_router(jobs.router, dependencies=[Depends(require_admin)])
    app.include_router(platform_settings.router, dependencies=[Depends(require_admin)])
    return app


def app_from_env() -> FastAPI:
    """Entry point for uvicorn (`--factory`): settings and logging come from the environment."""
    settings = Settings.from_env()
    configure_logging("api", settings.log_level_number)
    return create_app(settings, AuthSettings.from_env())
