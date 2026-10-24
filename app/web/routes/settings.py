"""Plattform-Einstellungen (requirements.md, "Settings"). Benutzer-Einstellungen follow with user management."""
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.services import extraction_service, platform_settings_service, seed_service, source_service
from app.services.platform_settings_service import InvalidSetting
from app.web.forms import form_values, wants_json
from app.web.templating import flash, name_list, templates

router = APIRouter(prefix="/settings/platform", tags=["settings"])

SAVED = "Gespeichert"


@router.get("")
def platform_settings(request: Request) -> Response:
    engine = request.app.state.engine
    context = {
        "settings": platform_settings_service.current(engine),
        "fields": platform_settings_service.FORM_FIELDS,
        "sources_seed": seed_service.sources_status(engine),
        "sources_without_region": len(source_service.list_sources(engine, region=source_service.NO_REGION)),
        "llm": extraction_service.llm_status(*request.app.state.llm),
    }
    return templates.TemplateResponse(request, "settings_platform.html", context)


@router.post("")
def save_platform_settings(request: Request, form: Annotated[dict[str, str], Depends(form_values)]) -> Response:
    """static/app.js saves in place and gets JSON; without JavaScript the page is shown again with a message.

    Each section's form sends only its own settings.
    """
    try:
        platform_settings_service.save(request.app.state.engine, form)
    except InvalidSetting as error:
        if wants_json(request):
            # 200 like the login form shown again with an error (routes/auth.py): browsers log every
            # 4xx answer as a console error, and an invalid entry is no error of the page.
            return JSONResponse({"error": str(error)})
        flash(request, str(error), "error")
    else:
        if wants_json(request):
            return JSONResponse({"status": SAVED})
        flash(request, SAVED)
    return RedirectResponse("/settings/platform", status_code=303)


@router.post("/seed/sources")
def import_source_catalogue(request: Request) -> Response:
    """The only way the starter catalogue gets in: an explicit admin action (architecture.md, "Seed Data")."""
    result = seed_service.import_sources(request.app.state.engine)
    text = f"Starterkatalog Quellen: {result.imported} importiert, {result.skipped} schon früher importiert."
    if result.name_taken:
        flash(request, f"{text} Nicht importiert, Name schon vergeben: {name_list(result.name_taken)}.", "error")
    else:
        flash(request, text)
    return RedirectResponse("/settings/platform#starterdaten", status_code=303)


@router.post("/seed/source-regions")
def take_catalogue_regions(request: Request) -> Response:
    """Regions for sources from before regions existed; an explicit admin action like the import."""
    given = seed_service.take_catalogue_regions(request.app.state.engine)
    flash(request, f"{given} {'Quelle hat' if given == 1 else 'Quellen haben'} die Region ihres Katalogeintrags erhalten.")
    return RedirectResponse("/settings/platform#starterdaten", status_code=303)
