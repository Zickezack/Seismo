"""Ereignisse: the most recently reported events, filtered and searched as the analyst chooses, and one event with its
reports, what they leave uncertain or contradict, how the market reacted and the earlier events comparable with it,
where the analyst merges similar events into it and splits off reports of another incident."""
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.db.models import WatchItemKind
from app.services import (
    entity_service,
    event_context_service,
    event_correction_service,
    event_service,
    event_study_service,
    impact_service,
    instrument_service,
    situation_service,
    watchlist_service,
)
from app.services.event_correction_service import InvalidReason
from app.web.templating import flash, name_list, templates

router = APIRouter(prefix="/events", tags=["events"])

NOT_FOUND = "Ereignis nicht gefunden."
EventIds = Annotated[list[int], Form(alias="event_id")]
CandidateIds = Annotated[list[int], Form(alias="candidate_id")]
Reason = Annotated[str, Form()]


@router.get("")
def event_list(request: Request) -> Response:
    """Filtered by a plain GET form: works without JavaScript, and the URL can be bookmarked."""
    engine = request.app.state.engine
    form = event_service.parse_filter(request.query_params)
    overview = event_service.overview(engine, form.criteria)
    context = {
        "overview": overview,
        "uncertainty": event_context_service.uncertainty_of(engine, [event.id for event in overview.events]),
        "filter": form,
        "choices": event_service.filter_choices(engine),
    }
    return templates.TemplateResponse(request, "events.html", context)


@router.get("/{event_id}")
def event_page(request: Request, event_id: int) -> Response:
    engine = request.app.state.engine
    detail = event_service.detail(engine, event_id)
    if detail is None:
        flash(request, NOT_FOUND, "error")
        return RedirectResponse("/events", status_code=303)
    entities = entity_service.of_event(engine, event_id)
    context = {
        "detail": detail,
        "similar": event_correction_service.similar_events(engine, event_id),
        "uncertainty": event_context_service.uncertainty_of(engine, [event_id])[event_id],
        "earlier": event_context_service.earlier_events(engine, event_id),
        "entities": entities,
        "instruments": instrument_service.of_entities(engine, [entity.id for entity in entities]),
        "reaction": event_study_service.of_event(engine, event_id),
        "impacts": impact_service.of_event(engine, event_id),
        "watch": watchlist_service.watching(engine, WatchItemKind.EVENT, event_id),
        "situations": situation_service.of_event(engine, event_id),
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
