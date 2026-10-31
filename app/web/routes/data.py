"""Datenbestand: what the data takes - now, per Quelle and day by day - and the reprocessing of runs made with an older
schema version or another model (userstories.md, Verdichtung und Daten-Lifecycle, story 2)."""
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.services import reprocessing_service, storage_service
from app.services.reprocessing_service import InvalidReprocessing
from app.web.forms import form_values
from app.web.templating import flash, name_list, templates

router = APIRouter(prefix="/data", tags=["data"])

FormValues = Annotated[dict[str, str], Depends(form_values)]
ReprocessingIds = Annotated[list[int], Form(alias="reprocessing_id")]
# The form's fields, kept in the URL of the page and of the way back after a start that failed.
CHOICE_FIELDS = ("group", "source_id", "period_start", "period_end")


@router.get("")
def data_page(request: Request, group: str = "", source_id: str = "", period_start: str = "", period_end: str = "") -> Response:
    """With a Stand chosen (a GET form, so the choice can be bookmarked) the page shows what it would take."""
    engine, model = request.app.state.engine, request.app.state.llm[1]
    choice = {"group": group, "source_id": source_id, "period_start": period_start, "period_end": period_end}
    error = None
    try:
        overview = reprocessing_service.overview(engine, model, choice)
    except InvalidReprocessing as invalid:
        overview, error = reprocessing_service.overview(engine, model), str(invalid)
    measurements = storage_service.history(engine)
    context = {
        "storage": storage_service.current(engine, request.app.state.backup_dir),
        "measurements": measurements,
        "growths": [growth for days in storage_service.GROWTH_PERIODS if (growth := storage_service.growth(measurements, days))],
        "overview": overview,
        "choice": choice,
        "choice_error": error,
    }
    return templates.TemplateResponse(request, "data.html", context)


@router.post("/reprocessings")
def start_reprocessing(request: Request, form: FormValues) -> Response:
    try:
        reprocessing = reprocessing_service.start(request.app.state.engine, form, request.app.state.llm[1])
    except InvalidReprocessing as error:
        flash(request, f"Neuverarbeitung nicht gestartet: {error}", "error")
        choice = urlencode({name: form.get(name, "") for name in CHOICE_FIELDS})
        return RedirectResponse(f"/data?{choice}#neu-verarbeiten", status_code=303)
    runs = "Lauf" if reprocessing.total == 1 else "Läufe"
    flash(request, f"Neuverarbeitung gestartet: {reprocessing.total} {runs} der {reprocessing.step_label}.")
    return RedirectResponse("/data#neuverarbeitungen", status_code=303)


@router.post("/reprocessings/cancel")
def cancel_reprocessings(request: Request, reprocessing_ids: ReprocessingIds = []) -> Response:
    result = reprocessing_service.cancel(request.app.state.engine, reprocessing_ids)
    if result.blocked:
        flash(request, f"Nichts abgebrochen, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{result.processed} {'Neuverarbeitung' if result.processed == 1 else 'Neuverarbeitungen'} abgebrochen.")
    else:
        flash(request, "Keine Neuverarbeitungen ausgewählt.", "error")
    return RedirectResponse("/data#neuverarbeitungen", status_code=303)
