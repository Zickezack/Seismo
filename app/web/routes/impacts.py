"""Wirkungsannahmen of an event (userstories.md, Markt- und Wirkungsmodell, story 1): confirm or reject the selected
ones, all or nothing; one on its own page with its versions, changed by the analyst; a new one by hand."""
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.services import impact_service
from app.services.impact_service import InvalidHypothesis
from app.web.templating import flash, name_list, templates

router = APIRouter(prefix="/events/{event_id}/impacts", tags=["impacts"])

HypothesisIds = Annotated[list[int], Form(alias="hypothesis_id")]
Refs = Annotated[list[str], Form()]
Text = Annotated[str, Form()]
FIELDS = ("target", "way", "direction", "horizon", "mechanism", "counterarguments", "classification", "note")


def _section(event_id: int) -> RedirectResponse:
    return RedirectResponse(f"/events/{event_id}#impacts", status_code=303)


@router.post("/confirm")
def confirm(request: Request, event_id: int, hypothesis_ids: HypothesisIds = [], note: Text = "") -> Response:
    try:
        result = impact_service.confirm(request.app.state.engine, event_id, hypothesis_ids, note)
    except InvalidHypothesis as error:
        flash(request, f"Nichts bestätigt. {error}", "error")
        return _section(event_id)
    return _reviewed(request, event_id, result, "bestätigt", "Bereits bestätigt")


@router.post("/reject")
def reject(request: Request, event_id: int, hypothesis_ids: HypothesisIds = [], note: Text = "") -> Response:
    try:
        result = impact_service.reject(request.app.state.engine, event_id, hypothesis_ids, note)
    except InvalidHypothesis as error:
        flash(request, f"Nichts verworfen. {error}", "error")
        return _section(event_id)
    return _reviewed(request, event_id, result, "verworfen", "Bereits verworfen")


def _reviewed(request: Request, event_id: int, result, verb: str, why: str) -> Response:
    if result.blocked:
        flash(request, f"Nichts {verb}. {why}: {name_list(result.blocked)}.", "error")
    elif result.processed:
        words = "Wirkungsannahme" if result.processed == 1 else "Wirkungsannahmen"
        flash(request, f"{result.processed} {words} {verb}.")
    else:
        flash(request, "Keine Wirkungsannahmen ausgewählt.", "error")
    return _section(event_id)


@router.get("/new")
def new_page(request: Request, event_id: int) -> Response:
    choices = impact_service.form_choices(request.app.state.engine, event_id)
    if choices is None:
        flash(request, "Ereignis nicht gefunden oder zusammengeführt.", "error")
        return RedirectResponse("/events", status_code=303)
    return _form_page(request, event_id, None, choices, {}, [], [])


@router.post("")
async def create(request: Request, event_id: int) -> Response:
    form = await request.form()
    values, support, against = _entries(form)
    engine = request.app.state.engine
    try:
        hypothesis_id = impact_service.create(engine, event_id, values, support, against)
    except InvalidHypothesis as error:
        choices = impact_service.form_choices(engine, event_id)
        if choices is None:
            flash(request, str(error), "error")
            return RedirectResponse("/events", status_code=303)
        return _form_page(request, event_id, None, choices, values, support, against, str(error))
    flash(request, "Wirkungsannahme angelegt.")
    return RedirectResponse(f"/events/{event_id}/impacts/{hypothesis_id}", status_code=303)


@router.get("/{hypothesis_id}")
def hypothesis_page(request: Request, event_id: int, hypothesis_id: int) -> Response:
    detail = impact_service.detail(request.app.state.engine, hypothesis_id)
    if detail is None or detail.hypothesis.event_id != event_id:
        flash(request, "Wirkungsannahme nicht gefunden.", "error")
        return _section(event_id)
    return _form_page(request, event_id, detail, detail.choices, {}, [], [])


@router.post("/{hypothesis_id}")
async def change(request: Request, event_id: int, hypothesis_id: int) -> Response:
    form = await request.form()
    values, support, against = _entries(form)
    engine = request.app.state.engine
    try:
        changed = impact_service.change(engine, hypothesis_id, values, support, against)
    except InvalidHypothesis as error:
        detail = impact_service.detail(engine, hypothesis_id)
        return _form_page(request, event_id, detail, detail.choices, values, support, against, str(error))
    if not changed:
        flash(request, "Wirkungsannahme nicht gefunden.", "error")
        return _section(event_id)
    flash(request, "Gespeichert: eine neue Version, bestätigt.")
    return RedirectResponse(f"/events/{event_id}/impacts/{hypothesis_id}", status_code=303)


def _entries(form) -> tuple[dict[str, str], list[str], list[str]]:
    values = {name: str(form.get(name) or "") for name in FIELDS}
    return values, [str(each) for each in form.getlist("support")], [str(each) for each in form.getlist("against")]


def _form_page(request, event_id, detail, choices, values, support, against, error=None) -> Response:
    """The page of one hypothesis - or a new one - with its form: the entries sent again after an error, else the
    latest version's."""
    current = detail.hypothesis if detail else None
    if current is not None and not values:
        values = {
            "direction": current.direction, "horizon": current.horizon, "mechanism": current.mechanism,
            "counterarguments": current.counterarguments, "classification": current.classification,
        }
        support = [each.key for each in current.support]
        against = [each.key for each in current.against]
    context = {
        "event_id": event_id, "detail": detail, "choices": choices, "values": values, "support": set(support),
        "against": set(against), "error": error, "directions": impact_service.DIRECTION_LABELS,
        "horizons": impact_service.HORIZON_LABELS, "classes": impact_service.CLASS_LABELS,
    }
    return templates.TemplateResponse(request, "impact.html", context, status_code=200)
