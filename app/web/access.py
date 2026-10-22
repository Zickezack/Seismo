"""Request-time access check.

Convention (architecture.md, "Codebase structure"): main.py includes every router with
Depends(require_admin), except the public ones it lists explicitly. A test calls every route
without a session and fails for any that answers.
"""
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

SESSION_KEY = "admin"
SESSION_COOKIE = "seismo_session"
# Counted from the login; Starlette does not renew the cookie on later requests.
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60


class NotAuthenticated(Exception):
    """Raised by require_admin; handled by redirect_to_login."""


def current_admin(request: Request) -> str | None:
    return request.session.get(SESSION_KEY)


def require_admin(request: Request) -> str:
    admin = current_admin(request)
    if not admin:
        raise NotAuthenticated
    return admin


async def redirect_to_login(request: Request, _error: NotAuthenticated) -> Response:
    """A browser opening a page goes to the login and back afterwards; anything else gets a 401."""
    if request.method in ("GET", "HEAD") and "text/html" in request.headers.get("accept", ""):
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?{urlencode({'next': target})}", status_code=303)
    return JSONResponse({"detail": "Nicht angemeldet"}, status_code=401)
