"""Wertpapiere: the stocks, ETFs and indices Seismo keeps prices of, each with its latest price; one Wertpapier with
its latest Kurspunkte, its daily prices as known at a day, and its corporate actions (Live-Kurse und
Kursdatenspeicher, story 1; Historische Markt- und Ereignisdaten, story 1)."""
from typing import Annotated
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.db.models import WatchItemKind
from app.services import entity_service, instrument_service, watchlist_service
from app.services.bulk import BulkResult
from app.services.instrument_service import InvalidInstrument
from app.web.forms import form_values
from app.web.templating import flash, name_list, templates

router = APIRouter(prefix="/instruments", tags=["instruments"])

NOT_FOUND = "Wertpapier nicht gefunden."
FormValues = Annotated[dict[str, str], Depends(form_values)]
InstrumentIds = Annotated[list[int], Form(alias="instrument_id")]
Back = Annotated[str, Form()]
# The list's filters, kept through a bulk action.
FILTERS = ("live", "source", "q")


@router.get("")
def instrument_list(request: Request, live: str = "", source: str = "", q: str = "") -> Response:
    """Filtered by a plain GET form: works without JavaScript, and the URL can be bookmarked."""
    engine = request.app.state.engine
    context = {
        "overview": instrument_service.overview(engine, bool(live), source, q),
        "live": bool(live),
        "source": source,
        "q": q,
        "back": urlencode({"live": "1" if live else "", "source": source, "q": q}),
        "sources": instrument_service.SOURCE_LABELS,
        "entered_sources": instrument_service.ENTERED_SOURCES,
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


@router.post("/live-on")
def live_on(request: Request, instrument_ids: InstrumentIds = [], back: Back = "") -> Response:
    return _bulk(request, instrument_service.set_live(request.app.state.engine, instrument_ids, True), "mit Live-Kurs", back)


@router.post("/live-off")
def live_off(request: Request, instrument_ids: InstrumentIds = [], back: Back = "") -> Response:
    return _bulk(request, instrument_service.set_live(request.app.state.engine, instrument_ids, False), "ohne Live-Kurs", back)


@router.post("/delete")
def delete(request: Request, instrument_ids: InstrumentIds = [], back: Back = "") -> Response:
    return _bulk(request, instrument_service.delete(request.app.state.engine, instrument_ids), "gelöscht", back)


@router.get("/{instrument_id}")
def instrument_page(request: Request, instrument_id: int, as_of: str = "") -> Response:
    engine = request.app.state.engine
    detail = instrument_service.detail(engine, instrument_id, instrument_service.parse_as_of(as_of))
    if detail is None:
        flash(request, NOT_FOUND, "error")
        return RedirectResponse("/instruments", status_code=303)
    context = {
        "detail": detail,
        "as_of": as_of if detail.as_of else "",
        "number": instrument_service.number_label,
        "kinds": instrument_service.KIND_LABELS,
        "entered_sources": instrument_service.ENTERED_SOURCES,
        "choices": entity_service.choices(engine),
        "watch": watchlist_service.watching(engine, WatchItemKind.INSTRUMENT, instrument_id),
    }
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


def _bulk(request: Request, result: BulkResult, verb: str, back: str) -> Response:
    if result.blocked:
        flash(request, f"Nichts {verb}, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{result.processed} {'Wertpapier' if result.processed == 1 else 'Wertpapiere'} {verb}.")
    else:
        flash(request, "Keine Wertpapiere ausgewählt.", "error")
    # Back to the list as it was filtered: only its own filters, nothing else the form carried.
    query = urlencode([(key, value) for key, value in parse_qsl(back) if key in FILTERS and value])
    return RedirectResponse(f"/instruments?{query}" if query else "/instruments", status_code=303)
