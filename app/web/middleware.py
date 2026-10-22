import logging
import time
import uuid

from fastapi import Request

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
HEALTH_PATH_PREFIX = "/health/"


async def log_requests(request: Request, call_next):
    """One structured log line per request, with the path only - query strings may carry search terms."""
    request_id = uuid.uuid4().hex
    fields = {"request_id": request_id, "method": request.method, "path": request.url.path}
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Request failed", extra=fields | {"status": 500})
        raise
    fields |= {"status": response.status_code, "duration_ms": round((time.perf_counter() - started) * 1000, 1)}
    response.headers[REQUEST_ID_HEADER] = request_id
    logger.log(_level_for(request.url.path, response.status_code), "Request handled", extra=fields)
    return response


def _level_for(path: str, status: int) -> int:
    if status >= 500:
        return logging.WARNING
    # Docker polls the health checks every few seconds; successful polls would drown out everything else.
    if path.startswith(HEALTH_PATH_PREFIX) and status < 400:
        return logging.DEBUG
    return logging.INFO
