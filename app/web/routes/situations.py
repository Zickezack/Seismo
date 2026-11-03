"""Lagen: the proposed ones to confirm or reject, the confirmed ones active or dormant; one Lage with the timeline of
its events, where the analyst confirms, removes and splits off events and merges other Lagen into it
(situation_service)."""
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.db.models import WatchItemKind
from app.services import situation_service, watchlist_service
from app.services.bulk import BulkResult
from app.services.situation_service import InvalidSituation
from app.web.forms import wants_json
from app.web.templating import flash, name_list, templates

router = APIRouter(prefix="/situations", tags=["situations"])

NOT_FOUND = "Lage nicht gefunden."
SAVED = "Gespeichert"
SituationIds = Annotated[list[int], Form(alias="situation_id")]
EventIds = Annotated[list[int], Form(alias="event_id")]
Text = Annotated[str, Form()]


@router.get("")
def situation_list(request: Request) -> Response:
    context = {"overview": situation_service.overview(request.app.state.engine)}
    return templates.TemplateResponse(request, "situations.html", context)


@router.post("/confirm")
def confirm(request: Request, situation_ids: SituationIds = []) -> Response:
    return _lagen(request, situation_service.confirm(request.app.state.engine, situation_ids), "bestätigt")


@router.post("/reject")
def reject(request: Request, situation_ids: SituationIds = []) -> Response:
    return _lagen(request, situation_service.reject(request.app.state.engine, situation_ids), "abgelehnt")


@router.post("/add")
def add_event(request: Request, situation: Text = "", event: Text = "") -> Response:
    """Zu Lage hinzufügen, from the event's page; back there."""
    page = f"/events/{event}#situations" if event.isdigit() else "/events"
    if not situation.isdigit() or not event.isdigit():
        flash(request, "Nicht hinzugefügt: bitte eine Lage wählen.", "error")
        return RedirectResponse(page, status_code=303)
    try:
        name = situation_service.add_event(request.app.state.engine, int(situation), int(event))
    except InvalidSituation as error:
        flash(request, f"Nicht hinzugefügt: {error}", "error")
    else:
        flash(request, f"Zur Lage {name} hinzugefügt.")
    return RedirectResponse(page, status_code=303)


@router.get("/{situation_id}")
def situation_page(request: Request, situation_id: int) -> Response:
    engine = request.app.state.engine
    detail = situation_service.detail(engine, situation_id)
    if detail is None:
        flash(request, NOT_FOUND, "error")
        return RedirectResponse("/situations", status_code=303)
    context = {"detail": detail, "watch": watchlist_service.watching(engine, WatchItemKind.SITUATION, situation_id)}
    return templates.TemplateResponse(request, "situation.html", context)


@router.post("/{situation_id}/describe")
def describe(request: Request, situation_id: int, name: Text = "", description: Text = "") -> Response:
    """Saved in place (static/app.js, data-inline-save); without JavaScript the page comes again."""
    try:
        error = None if situation_service.describe(request.app.state.engine, situation_id, name, description) else NOT_FOUND
    except InvalidSituation as invalid:
        error = str(invalid)
    if wants_json(request):
        return JSONResponse({"error": error} if error else {"status": SAVED})
    flash(request, error or SAVED, "error" if error else "success")
    return RedirectResponse(f"/situations/{situation_id}", status_code=303)


@router.post("/{situation_id}/confirm-events")
def confirm_events(request: Request, situation_id: int, event_ids: EventIds = []) -> Response:
    result = situation_service.confirm_events(request.app.state.engine, situation_id, event_ids)
    return _events(request, situation_id, result, "bestätigt")


@router.post("/{situation_id}/remove-events")
def remove_events(request: Request, situation_id: int, event_ids: EventIds = []) -> Response:
    result = situation_service.remove_events(request.app.state.engine, situation_id, event_ids)
    return _events(request, situation_id, result, "entfernt")


@router.post("/{situation_id}/split")
def split(request: Request, situation_id: int, event_ids: EventIds = [], name: Text = "") -> Response:
    """Als neue Lage abspalten: the selected events become a new Lage of that name."""
    try:
        result = situation_service.split(request.app.state.engine, situation_id, event_ids, name)
    except InvalidSituation as error:
        flash(request, f"Nichts abgespalten. {error}", "error")
        return RedirectResponse(f"/situations/{situation_id}#events", status_code=303)
    if result.blocked:
        flash(request, f"Nichts abgespalten, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{_events_word(result.processed)} als neue Lage abgespalten.")
        return RedirectResponse(f"/situations/{result.situation_id}", status_code=303)
    else:
        flash(request, "Keine Ereignisse ausgewählt.", "error")
    return RedirectResponse(f"/situations/{situation_id}#events", status_code=303)


@router.post("/{situation_id}/merge")
def merge(request: Request, situation_id: int, situation_ids: SituationIds = []) -> Response:
    """In diese Lage zusammenführen."""
    result = situation_service.merge(request.app.state.engine, situation_id, situation_ids)
    if result.blocked:
        flash(request, f"Nichts zusammengeführt, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{result.processed} {'Lage' if result.processed == 1 else 'Lagen'} in diese zusammengeführt.")
    else:
        flash(request, "Keine Lagen ausgewählt.", "error")
    return RedirectResponse(f"/situations/{situation_id}#merge", status_code=303)


def _lagen(request: Request, result: BulkResult, verb: str) -> Response:
    if result.blocked:
        flash(request, f"Nichts {verb}, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{result.processed} {'Lage' if result.processed == 1 else 'Lagen'} {verb}.")
    else:
        flash(request, "Keine Lagen ausgewählt.", "error")
    return RedirectResponse("/situations", status_code=303)


def _events(request: Request, situation_id: int, result: BulkResult, verb: str) -> Response:
    if result.blocked:
        flash(request, f"Nichts {verb}, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{_events_word(result.processed)} {verb}.")
    else:
        flash(request, "Keine Ereignisse ausgewählt.", "error")
    return RedirectResponse(f"/situations/{situation_id}#events", status_code=303)


def _events_word(count: int) -> str:
    return f"{count} {'Ereignis' if count == 1 else 'Ereignisse'}"
