"""Login and logout. Public on purpose: logging out without a session changes nothing."""
import logging
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import RedirectResponse, Response

from app.services.auth_service import credentials_match, safe_next_path
from app.web.access import SESSION_KEY, current_admin
from app.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

LOGIN_FAILED = "Benutzername oder Passwort ist falsch."


@router.get("/login")
def login_form(request: Request, next_path: Annotated[str, Query(alias="next")] = "/") -> Response:
    if current_admin(request):
        return RedirectResponse(safe_next_path(next_path), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": safe_next_path(next_path)})


@router.post("/login")
def login(
    request: Request,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next_path: Annotated[str, Form(alias="next")] = "/",
) -> Response:
    if not credentials_match(request.app.state.auth, username, password):
        # Without the username: a password typed into the wrong field would end up in the log.
        logger.warning("Login failed")
        context = {"next": safe_next_path(next_path), "username": username, "error": LOGIN_FAILED}
        # 200 like any form shown again with an error: a 401 needs a WWW-Authenticate challenge,
        # which a login form has none of, and browsers log it as a console error.
        return templates.TemplateResponse(request, "login.html", context)
    request.session.clear()
    request.session[SESSION_KEY] = request.app.state.auth.admin_username
    logger.info("Login succeeded")
    return RedirectResponse(safe_next_path(next_path), status_code=303)


@router.post("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
