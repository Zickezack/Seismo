"""Ereignisse: the most recently reported events, and one event with its reports. Merging and splitting follow with
story 3 of their epic."""
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from app.services import event_service
from app.web.templating import flash, templates

router = APIRouter(prefix="/events", tags=["events"])

NOT_FOUND = "Ereignis nicht gefunden."


@router.get("")
def event_list(request: Request) -> Response:
    context = {"overview": event_service.overview(request.app.state.engine)}
    return templates.TemplateResponse(request, "events.html", context)


@router.get("/{event_id}")
def event_page(request: Request, event_id: int) -> Response:
    detail = event_service.detail(request.app.state.engine, event_id)
    if detail is None:
        flash(request, NOT_FOUND, "error")
        return RedirectResponse("/events", status_code=303)
    return templates.TemplateResponse(request, "event.html", {"detail": detail})
