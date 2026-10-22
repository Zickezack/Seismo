"""Health checks for Docker. Public on purpose: they reveal no data, only up or down."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.services.health_service import check_readiness

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def live() -> dict:
    """The process answers. Says nothing about the database."""
    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request) -> JSONResponse:
    report = check_readiness(request.app.state.engine, request.app.state.expected_revision)
    if not report.database_reachable:
        schema = "unknown"
    else:
        schema = "current" if report.schema_current else "outdated"
    body = {
        "status": "ok" if report.ready else "unavailable",
        "database": "ok" if report.database_reachable else "unreachable",
        "schema": schema,
    }
    return JSONResponse(body, status_code=200 if report.ready else 503)
