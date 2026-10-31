"""Entitäten: the entities the reports name, and one entity with its name forms and reports, where the analyst
confirms uncertain name forms, splits off wrong ones and merges similar entities into it."""
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.db.models import ReviewStatus
from app.services import entity_correction_service, entity_service, relationship_service
from app.services.relationship_service import InvalidRelationship
from app.web.templating import flash, name_list, templates

router = APIRouter(prefix="/entities", tags=["entities"])

NOT_FOUND = "Entität nicht gefunden."
AliasIds = Annotated[list[int], Form(alias="alias_id")]
EntityIds = Annotated[list[int], Form(alias="entity_id")]
RelationshipIds = Annotated[list[int], Form(alias="relationship_id")]
Text = Annotated[str, Form()]


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
    context = {
        "detail": detail,
        "similar": entity_correction_service.similar_entities(engine, entity_id),
        "relationships": relationship_service.of_entity(engine, entity_id),
        "roles": relationship_service.role_choices(),
        "choices": entity_service.choices(engine),
    }
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


@router.post("/{entity_id}/relationships")
def add_relationship(
    request: Request, entity_id: int, role: Text = "", other: Text = "", valid_from: Text = "", valid_to: Text = "", note: Text = ""
) -> Response:
    """Beziehung hinzufügen: the analyst's own, confirmed, with its source."""
    try:
        relationship_id = relationship_service.add(request.app.state.engine, entity_id, role, other, valid_from, valid_to, note)
    except InvalidRelationship as error:
        flash(request, f"Keine Beziehung erfasst. {error}", "error")
    else:
        flash(request, f"Beziehung {relationship_id} erfasst.")
    return RedirectResponse(f"/entities/{entity_id}#relationships", status_code=303)


@router.post("/{entity_id}/relationships/confirm")
def confirm_relationships(request: Request, entity_id: int, relationship_ids: RelationshipIds = [], note: Text = "") -> Response:
    return _review(request, entity_id, relationship_ids, ReviewStatus.CONFIRMED, note, "bestätigt")


@router.post("/{entity_id}/relationships/dispute")
def dispute_relationships(request: Request, entity_id: int, relationship_ids: RelationshipIds = [], note: Text = "") -> Response:
    return _review(request, entity_id, relationship_ids, ReviewStatus.DISPUTED, note, "bestritten")


def _review(request: Request, entity_id: int, relationship_ids: list[int], status: str, note: str, verb: str) -> Response:
    try:
        result = relationship_service.review(request.app.state.engine, entity_id, relationship_ids, status, note)
    except InvalidRelationship as error:
        flash(request, f"Nichts {verb}. {error}", "error")
    else:
        if result.blocked:
            flash(request, f"Nichts {verb}, blockiert: {name_list(result.blocked)}.", "error")
        elif result.processed:
            flash(request, f"{result.processed} {'Beziehung' if result.processed == 1 else 'Beziehungen'} {verb}.")
        else:
            flash(request, "Keine Beziehungen ausgewählt.", "error")
    return RedirectResponse(f"/entities/{entity_id}#relationships", status_code=303)
