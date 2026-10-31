"""Ereignisse: the most recently reported events, and one event with its reports, where the analyst merges similar
events into it and splits off reports of another incident."""
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.services import entity_service, event_correction_service, event_service
from app.services.event_correction_service import InvalidReason
from app.web.templating import flash, name_list, templates

router = APIRouter(prefix="/events", tags=["events"])

NOT_FOUND = "Ereignis nicht gefunden."
EventIds = Annotated[list[int], Form(alias="event_id")]
CandidateIds = Annotated[list[int], Form(alias="candidate_id")]
Reason = Annotated[str, Form()]


@router.get("")
def event_list(request: Request) -> Response:
    context = {"overview": event_service.overview(request.app.state.engine)}
    return templates.TemplateResponse(request, "events.html", context)


@router.get("/{event_id}")
def event_page(request: Request, event_id: int) -> Response:
    engine = request.app.state.engine
    detail = event_service.detail(engine, event_id)
    if detail is None:
        flash(request, NOT_FOUND, "error")
        return RedirectResponse("/events", status_code=303)
    context = {
        "detail": detail,
        "similar": event_correction_service.similar_events(engine, event_id),
        "entities": entity_service.of_event(engine, event_id),
    }
    return templates.TemplateResponse(request, "event.html", context)


@router.post("/{event_id}/merge")
def merge(request: Request, event_id: int, other_ids: EventIds = [], reason: Reason = "") -> Response:
    """Zusammenführen: the selected similar events go into this one."""
    try:
        result = event_correction_service.merge(request.app.state.engine, event_id, other_ids, reason)
    except InvalidReason as error:
        flash(request, f"Nichts zusammengeführt. {error}", "error")
    else:
        if result.blocked:
            flash(request, f"Nichts zusammengeführt, blockiert: {name_list(result.blocked)}.", "error")
        elif result.processed:
            flash(request, f"{result.processed} {'Ereignis' if result.processed == 1 else 'Ereignisse'} in dieses zusammengeführt.")
        else:
            flash(request, "Keine Ereignisse ausgewählt.", "error")
    return RedirectResponse(f"/events/{event_id}#similar", status_code=303)


@router.post("/{event_id}/split")
def split(request: Request, event_id: int, candidate_ids: CandidateIds = [], reason: Reason = "") -> Response:
    """Abspalten: the selected reports become a new event."""
    try:
        result = event_correction_service.split(request.app.state.engine, event_id, candidate_ids, reason)
    except InvalidReason as error:
        flash(request, f"Nichts abgespalten. {error}", "error")
    else:
        if result.blocked:
            flash(request, f"Nichts abgespalten, blockiert: {name_list(result.blocked)}.", "error")
        elif result.processed:
            reports = "Bericht" if result.processed == 1 else "Berichte"
            flash(request, f"{result.processed} {reports} als neues Ereignis {result.event_id} abgespalten.")
        else:
            flash(request, "Keine Berichte ausgewählt.", "error")
    return RedirectResponse(f"/events/{event_id}#reports", status_code=303)
