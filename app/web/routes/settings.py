"""Plattform-Einstellungen (requirements.md, "Settings"). Benutzer-Einstellungen follow with user management."""
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.services import platform_settings_service
from app.services.platform_settings_service import InvalidSetting
from app.web.templating import flash, templates

router = APIRouter(prefix="/settings/platform", tags=["settings"])

SAVED = "Gespeichert"


async def form_values(request: Request) -> dict[str, str]:
    """Every text field of the posted form; each section's form sends only its own settings."""
    form = await request.form()
    return {name: value for name, value in form.items() if isinstance(value, str)}


@router.get("")
def platform_settings(request: Request) -> Response:
    context = {
        "settings": platform_settings_service.current(request.app.state.engine),
        "fields": platform_settings_service.FORM_FIELDS,
    }
    return templates.TemplateResponse(request, "settings_platform.html", context)


@router.post("")
def save_platform_settings(request: Request, form: Annotated[dict[str, str], Depends(form_values)]) -> Response:
    """static/app.js saves in place and gets JSON; without JavaScript the page is shown again with a message."""
    wants_json = "application/json" in request.headers.get("accept", "")
    try:
        platform_settings_service.save(request.app.state.engine, form)
    except InvalidSetting as error:
        if wants_json:
            # 200 like the login form shown again with an error (routes/auth.py): browsers log every
            # 4xx answer as a console error, and an invalid entry is no error of the page.
            return JSONResponse({"error": str(error)})
        flash(request, str(error), "error")
    else:
        if wants_json:
            return JSONResponse({"status": SAVED})
        flash(request, SAVED)
    return RedirectResponse("/settings/platform", status_code=303)
