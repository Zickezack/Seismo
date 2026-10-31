"""Entitäten: the entities the reports name, and one entity with its name forms and reports, where the analyst
confirms uncertain name forms, splits off wrong ones and merges similar entities into it."""
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.services import entity_correction_service, entity_service
from app.web.templating import flash, name_list, templates

router = APIRouter(prefix="/entities", tags=["entities"])

NOT_FOUND = "Entität nicht gefunden."
AliasIds = Annotated[list[int], Form(alias="alias_id")]
EntityIds = Annotated[list[int], Form(alias="entity_id")]


@router.get("")
def entity_list(request: Request, kind: str = "", uncertain: str = "", q: str = "") -> Response:
    """Filtered by a plain GET form: works without JavaScript, and the URL can be bookmarked."""
    overview = entity_service.overview(request.app.state.engine, kind, bool(uncertain), q)
    context = {"overview": overview, "kind": kind, "uncertain": bool(uncertain), "q": q, "kinds": entity_service.KIND_LABELS}
    return templates.TemplateResponse(request, "entities.html", context)


@router.get("/{entity_id}")
def entity_page(request: Request, entity_id: int) -> Response:
    engine = request.app.state.engine
    detail = entity_service.detail(engine, entity_id)
    if detail is None:
        flash(request, NOT_FOUND, "error")
        return RedirectResponse("/entities", status_code=303)
    context = {"detail": detail, "similar": entity_correction_service.similar_entities(engine, entity_id)}
    return templates.TemplateResponse(request, "entity.html", context)


@router.post("/{entity_id}/confirm")
def confirm(request: Request, entity_id: int, alias_ids: AliasIds = []) -> Response:
    """Bestätigen: the selected uncertain name forms are this entity's."""
    result = entity_correction_service.confirm(request.app.state.engine, entity_id, alias_ids)
    if result.blocked:
        flash(request, f"Nichts bestätigt, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{result.processed} {'Namensform' if result.processed == 1 else 'Namensformen'} bestätigt.")
    elif alias_ids:
        flash(request, "Die ausgewählten Namensformen waren schon sicher.")
    else:
        flash(request, "Keine Namensformen ausgewählt.", "error")
    return RedirectResponse(f"/entities/{entity_id}#aliases", status_code=303)


@router.post("/{entity_id}/split")
def split(request: Request, entity_id: int, alias_ids: AliasIds = []) -> Response:
    """Abtrennen: the selected name forms become an entity of their own."""
    result = entity_correction_service.split(request.app.state.engine, entity_id, alias_ids)
    if result.blocked:
        flash(request, f"Nichts abgetrennt, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        forms = "Namensform" if result.processed == 1 else "Namensformen"
        flash(request, f"{result.processed} {forms} als neue Entität {result.entity_id} abgetrennt.")
    else:
        flash(request, "Keine Namensformen ausgewählt.", "error")
    return RedirectResponse(f"/entities/{entity_id}#aliases", status_code=303)


@router.post("/{entity_id}/merge")
def merge(request: Request, entity_id: int, other_ids: EntityIds = []) -> Response:
    """Zusammenführen: the selected similar entities go into this one."""
    result = entity_correction_service.merge(request.app.state.engine, entity_id, other_ids)
    if result.blocked:
        flash(request, f"Nichts zusammengeführt, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{result.processed} {'Entität' if result.processed == 1 else 'Entitäten'} in diese zusammengeführt.")
    else:
        flash(request, "Keine Entitäten ausgewählt.", "error")
    return RedirectResponse(f"/entities/{entity_id}#similar", status_code=303)
