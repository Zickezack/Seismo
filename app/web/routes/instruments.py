"""Wertpapiere: the stocks, ETFs and indices whose latest prices the worker fetches, each with its latest price; one
Wertpapier with its latest Kurspunkte (Live-Kurse und Kursdatenspeicher, story 1)."""
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.services import entity_service, instrument_service
from app.services.bulk import BulkResult
from app.services.instrument_service import InvalidInstrument
from app.web.forms import form_values
from app.web.templating import flash, name_list, templates

router = APIRouter(prefix="/instruments", tags=["instruments"])

NOT_FOUND = "Wertpapier nicht gefunden."
FormValues = Annotated[dict[str, str], Depends(form_values)]
InstrumentIds = Annotated[list[int], Form(alias="instrument_id")]


@router.get("")
def instrument_list(request: Request) -> Response:
    engine = request.app.state.engine
    context = {
        "overview": instrument_service.overview(engine),
        "kinds": instrument_service.KIND_LABELS,
        "choices": entity_service.choices(engine),
        "form": {},
    }
    return templates.TemplateResponse(request, "instruments.html", context)


@router.post("")
def create_instrument(request: Request, form: FormValues) -> Response:
    try:
        instrument = instrument_service.create(request.app.state.engine, form)
    except InvalidInstrument as error:
        flash(request, f"Nicht erfasst: {error}", "error")
        return RedirectResponse("/instruments#new", status_code=303)
    flash(request, f"{instrument.name} ({instrument.code}) erfasst.")
    return RedirectResponse(f"/instruments/{instrument.id}", status_code=303)


@router.post("/activate")
def activate(request: Request, instrument_ids: InstrumentIds = []) -> Response:
    return _bulk(request, instrument_service.set_active(request.app.state.engine, instrument_ids, True), "aktiviert")


@router.post("/deactivate")
def deactivate(request: Request, instrument_ids: InstrumentIds = []) -> Response:
    return _bulk(request, instrument_service.set_active(request.app.state.engine, instrument_ids, False), "deaktiviert")


@router.post("/delete")
def delete(request: Request, instrument_ids: InstrumentIds = []) -> Response:
    return _bulk(request, instrument_service.delete(request.app.state.engine, instrument_ids), "gelöscht")


@router.get("/{instrument_id}")
def instrument_page(request: Request, instrument_id: int) -> Response:
    engine = request.app.state.engine
    detail = instrument_service.detail(engine, instrument_id)
    if detail is None:
        flash(request, NOT_FOUND, "error")
        return RedirectResponse("/instruments", status_code=303)
    context = {"detail": detail, "kinds": instrument_service.KIND_LABELS, "choices": entity_service.choices(engine)}
    return templates.TemplateResponse(request, "instrument.html", context)


@router.post("/{instrument_id}")
def save_instrument(request: Request, instrument_id: int, form: FormValues) -> Response:
    try:
        instrument = instrument_service.update(request.app.state.engine, instrument_id, form)
    except InvalidInstrument as error:
        flash(request, f"Nicht gespeichert: {error}", "error")
        return RedirectResponse(f"/instruments/{instrument_id}#settings", status_code=303)
    if instrument is None:
        flash(request, NOT_FOUND, "error")
        return RedirectResponse("/instruments", status_code=303)
    flash(request, "Gespeichert.")
    return RedirectResponse(f"/instruments/{instrument_id}", status_code=303)


def _bulk(request: Request, result: BulkResult, verb: str) -> Response:
    if result.blocked:
        flash(request, f"Nichts {verb}, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{result.processed} {'Wertpapier' if result.processed == 1 else 'Wertpapiere'} {verb}.")
    else:
        flash(request, "Keine Wertpapiere ausgewählt.", "error")
    return RedirectResponse("/instruments", status_code=303)
