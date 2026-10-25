"""Quellen: the list with its bulk actions, a form for a new source, a page that edits one in place and starts
and cancels its backfills."""
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.services import article_service, backfill_service, ingestion_service, platform_settings_service, source_service
from app.services.backfill_service import InvalidBackfill
from app.services.bulk import BulkResult
from app.services.source_service import InvalidSource, Source
from app.web.forms import form_values, wants_json
from app.web.templating import flash, name_list, templates

router = APIRouter(prefix="/sources", tags=["sources"])

SAVED = "Gespeichert"
NOT_FOUND = "Quelle nicht gefunden."
SourceIds = Annotated[list[int], Form(alias="source_id")]
BackfillIds = Annotated[list[int], Form(alias="backfill_id")]
# What the backfill form suggests: the last month, within the source's reach.
DEFAULT_BACKFILL_DAYS = 30
FormValues = Annotated[dict[str, str], Depends(form_values)]


@router.get("")
def source_list(request: Request, region: str | None = None, sort: str | None = None) -> Response:
    """A plain GET filter like the article list: works without JavaScript, the URL can be bookmarked."""
    engine = request.app.state.engine
    context = {
        "sources": source_service.list_sources(engine, region, sort),
        "region_filter": region if region in source_service.REGIONS or region == source_service.NO_REGION else "",
        "sort": sort if sort in source_service.SORT_ORDERS else "name",
        "regions": source_service.REGIONS,
        "no_region": source_service.NO_REGION,
        "no_region_label": source_service.NO_REGION_LABEL,
        "sort_orders": source_service.SORT_ORDERS,
        "article_counts": article_service.count_by_source(engine),
        "blocked_counts": article_service.blocked_by_source(engine),
        "contact_missing": not request.app.state.fetch_contact,
    }
    return templates.TemplateResponse(request, "sources.html", context)


@router.get("/new")
def new_source(request: Request) -> Response:
    return _form_page(request, source_service.NEW_SOURCE_FORM)


@router.post("")
def create_source(request: Request, form: FormValues) -> Response:
    try:
        source = source_service.create(request.app.state.engine, form)
    except InvalidSource as error:
        # Shown again with the entries and the error, 200 like the login form (routes/auth.py).
        return _form_page(request, form, error=str(error))
    flash(request, f"Quelle „{source.name}“ angelegt.")
    return RedirectResponse("/sources", status_code=303)


# The bulk routes come before /{source_id}, which would otherwise take "delete" for an id.
@router.post("/delete")
def delete_sources(request: Request, source_ids: SourceIds = []) -> Response:
    result = source_service.delete(request.app.state.engine, source_ids)
    return _after_bulk_action(request, result, "gelöscht")


@router.post("/activate")
def activate_sources(request: Request, source_ids: SourceIds = []) -> Response:
    result = source_service.set_active(request.app.state.engine, source_ids, True)
    return _after_bulk_action(request, result, "aktiviert")


@router.post("/deactivate")
def deactivate_sources(request: Request, source_ids: SourceIds = []) -> Response:
    result = source_service.set_active(request.app.state.engine, source_ids, False)
    return _after_bulk_action(request, result, "deaktiviert")


@router.post("/fetch")
def fetch_sources(request: Request, source_ids: SourceIds = []) -> Response:
    result = ingestion_service.fetch_now(request.app.state.engine, source_ids)
    return _after_bulk_action(request, result, "zum Abruf eingereiht")


@router.post("/set-region")
def set_region(request: Request, source_ids: SourceIds = [], region: Annotated[str, Form()] = "") -> Response:
    try:
        result = source_service.set_region(request.app.state.engine, source_ids, region)
    except InvalidSource as error:
        flash(request, str(error), "error")
        return RedirectResponse("/sources", status_code=303)
    return _after_bulk_action(request, result, f"der Region {source_service.REGIONS[region]} zugeordnet")


@router.post("/refetch-blocked")
def refetch_blocked(request: Request, source_ids: SourceIds = []) -> Response:
    result = ingestion_service.refetch_blocked(request.app.state.engine, source_ids)
    if result.blocked:
        flash(request, f"Nichts eingereiht, blockiert: {name_list(result.blocked)}.", "error")
    elif not source_ids:
        flash(request, "Keine Quellen ausgewählt.", "error")
    elif result.processed == 1:
        flash(request, "1 blockierter Volltext erneut eingereiht.")
    elif result.processed:
        flash(request, f"{result.processed} blockierte Volltexte erneut eingereiht.")
    else:
        flash(request, "Die ausgewählten Quellen haben keine blockierten Volltexte.")
    return RedirectResponse("/sources", status_code=303)


@router.get("/{source_id}")
def edit_source(request: Request, source_id: int) -> Response:
    source = source_service.get(request.app.state.engine, source_id)
    if source is None:
        flash(request, NOT_FOUND, "error")
        return RedirectResponse("/sources", status_code=303)
    return _form_page(request, source, source=source)


@router.post("/{source_id}/backfills")
def start_backfill(request: Request, source_id: int, form: FormValues) -> Response:
    try:
        backfill = backfill_service.start(request.app.state.engine, source_id, form)
    except InvalidBackfill as error:
        flash(request, f"Backfill nicht gestartet: {error}", "error")
    else:
        flash(request, f"Backfill gestartet: {backfill.first_day:%d.%m.%Y} bis {backfill.last_day:%d.%m.%Y}.")
    return RedirectResponse(f"/sources/{source_id}#backfill", status_code=303)


@router.post("/{source_id}/backfills/cancel")
def cancel_backfills(request: Request, source_id: int, backfill_ids: BackfillIds = []) -> Response:
    result = backfill_service.cancel(request.app.state.engine, source_id, backfill_ids)
    if result.blocked:
        flash(request, f"Nichts abgebrochen, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{result.processed} {'Backfill' if result.processed == 1 else 'Backfills'} abgebrochen.")
    else:
        flash(request, "Keine Backfills ausgewählt.", "error")
    return RedirectResponse(f"/sources/{source_id}#backfill", status_code=303)


@router.post("/{source_id}")
def save_source(request: Request, source_id: int, form: FormValues) -> Response:
    """static/app.js saves in place and gets JSON; without JavaScript the page is shown again with a message."""
    try:
        source = source_service.update(request.app.state.engine, source_id, form)
    except InvalidSource as error:
        # 200 with the error, never 4xx (architecture.md, "Codebase structure").
        return _answer(request, f"/sources/{source_id}", error=str(error))
    if source is None:
        return _answer(request, "/sources", error=NOT_FOUND)
    return _answer(request, f"/sources/{source_id}")


def _form_page(
    request: Request, values: Mapping[str, object] | Source, *, source: Source | None = None, error: str | None = None
) -> Response:
    """The form for a new source (without `source`) or the edit page of one, with its backfills."""
    engine = request.app.state.engine
    context = {
        "values": values,
        "source": source,
        "error": error,
        "adapter_types": source_service.ADAPTER_TYPES,
        "languages": source_service.LANGUAGES,
        "regions": source_service.REGIONS,
        "qualities": source_service.QUALITIES,
        "no_language_label": source_service.NO_LANGUAGE_LABEL,
        "fetch_interval": source_service.FETCH_INTERVAL_MINUTES,
        "retention": source_service.RAW_TEXT_RETENTION_DAYS,
        "min_request_interval": source_service.MIN_REQUEST_INTERVAL_SECONDS,
        # What an empty "Mindestabstand" means.
        "platform_min_interval": platform_settings_service.current(engine).domain_min_interval_seconds,
        "max_lengths": {
            "name": source_service.MAX_NAME_LENGTH,
            "url": source_service.MAX_URL_LENGTH,
            "query": source_service.MAX_QUERY_LENGTH,
            "terms_note": source_service.MAX_TERMS_NOTE_LENGTH,
        },
    }
    if source is not None:
        now = datetime.now(UTC)
        earliest = backfill_service.earliest_day(source, now)
        today = now.date()
        suggested_start = today - timedelta(days=DEFAULT_BACKFILL_DAYS)
        context |= {
            "backfill_offered": backfill_service.offered(source),
            "backfills": backfill_service.list_for_source(engine, source.id),
            "backfill_earliest": earliest,
            "backfill_today": today,
            "backfill_start": max(suggested_start, earliest) if earliest else suggested_start,
        }
    return templates.TemplateResponse(request, "source_form.html", context)


def _answer(request: Request, page: str, *, error: str | None = None) -> Response:
    if wants_json(request):
        return JSONResponse({"error": error} if error else {"status": SAVED})
    flash(request, error or SAVED, "error" if error else "success")
    return RedirectResponse(page, status_code=303)


def _after_bulk_action(request: Request, result: BulkResult, done: str) -> RedirectResponse:
    if result.blocked:
        flash(request, f"Nichts {done}, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{_sources(result.processed)} {done}.")
    else:
        flash(request, "Keine Quellen ausgewählt.", "error")
    return RedirectResponse("/sources", status_code=303)


def _sources(count: int) -> str:
    return f"{count} Quelle" if count == 1 else f"{count} Quellen"
