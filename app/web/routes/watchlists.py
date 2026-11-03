"""Watchlisten: what the analyst follows, each with its entities, Wertpapiere, events, types and countries
(watchlist_service). Entities, events and Wertpapiere are added from their own pages."""
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.services import watchlist_service
from app.services.extraction_service import EVENT_TYPE_LABELS
from app.services.watchlist_service import InvalidWatchlist
from app.web.forms import wants_json
from app.web.templating import flash, name_list, templates

router = APIRouter(prefix="/watchlists", tags=["watchlists"])

NOT_FOUND = "Watchlist nicht gefunden."
SAVED = "Gespeichert"
WatchlistIds = Annotated[list[int], Form(alias="watchlist_id")]
ItemIds = Annotated[list[int], Form(alias="item_id")]
Text = Annotated[str, Form()]


@router.get("")
def watchlist_list(request: Request) -> Response:
    context = {"watchlists": watchlist_service.overview(request.app.state.engine)}
    return templates.TemplateResponse(request, "watchlists.html", context)


@router.post("")
def create_watchlist(request: Request, name: Text = "") -> Response:
    try:
        watchlist = watchlist_service.create(request.app.state.engine, name)
    except InvalidWatchlist as error:
        flash(request, f"Nicht angelegt: {error}", "error")
        return RedirectResponse("/watchlists#new", status_code=303)
    flash(request, f"Watchlist {watchlist.name} angelegt.")
    return RedirectResponse(f"/watchlists/{watchlist.id}", status_code=303)


@router.post("/delete")
def delete(request: Request, watchlist_ids: WatchlistIds = []) -> Response:
    result = watchlist_service.delete(request.app.state.engine, watchlist_ids)
    if result.blocked:
        flash(request, f"Nichts gelöscht, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{result.processed} {'Watchlist' if result.processed == 1 else 'Watchlists'} gelöscht.")
    else:
        flash(request, "Keine Watchlists ausgewählt.", "error")
    return RedirectResponse("/watchlists", status_code=303)


@router.post("/follow")
def follow(request: Request, watchlist: Text = "", kind: Text = "", target: Text = "", back: Text = "") -> Response:
    """Zur Watchlist hinzufügen: from the page of an entity, an event or a Wertpapier, or the watchlist's own for a
    type or a country; back to the page it came from."""
    page = back if back.startswith("/") and not back.startswith("//") else "/watchlists"
    if not watchlist.isdigit():
        flash(request, "Nicht hinzugefügt: bitte eine Watchlist wählen.", "error")
        return RedirectResponse(page, status_code=303)
    try:
        name, added = watchlist_service.add(request.app.state.engine, int(watchlist), kind, target)
    except InvalidWatchlist as error:
        flash(request, f"Nicht hinzugefügt: {error}", "error")
    else:
        flash(request, f"Zur Watchlist {name} hinzugefügt." if added else f"Die Watchlist {name} enthält es schon.")
    return RedirectResponse(page, status_code=303)


@router.get("/{watchlist_id}")
def watchlist_page(request: Request, watchlist_id: int) -> Response:
    detail = watchlist_service.detail(request.app.state.engine, watchlist_id)
    if detail is None:
        flash(request, NOT_FOUND, "error")
        return RedirectResponse("/watchlists", status_code=303)
    context = {
        "detail": detail,
        "kinds": watchlist_service.KIND_LABELS,
        "kind_order": watchlist_service.KIND_ORDER,
        "types": EVENT_TYPE_LABELS,
    }
    return templates.TemplateResponse(request, "watchlist.html", context)


@router.post("/{watchlist_id}/rename")
def rename(request: Request, watchlist_id: int, name: Text = "") -> Response:
    """Saved in place (static/app.js, data-inline-save); without JavaScript the page comes again."""
    page = f"/watchlists/{watchlist_id}"
    try:
        renamed = watchlist_service.rename(request.app.state.engine, watchlist_id, name)
    except InvalidWatchlist as error:
        error_text = str(error)
    else:
        error_text = None if renamed else NOT_FOUND
    if wants_json(request):
        return JSONResponse({"error": error_text} if error_text else {"status": SAVED})
    flash(request, error_text or SAVED, "error" if error_text else "success")
    return RedirectResponse(page, status_code=303)


@router.post("/{watchlist_id}/remove")
def remove_items(request: Request, watchlist_id: int, item_ids: ItemIds = []) -> Response:
    result = watchlist_service.remove_items(request.app.state.engine, watchlist_id, item_ids)
    if result.blocked:
        flash(request, f"Nichts entfernt, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{result.processed} {'Eintrag' if result.processed == 1 else 'Einträge'} entfernt.")
    else:
        flash(request, "Keine Einträge ausgewählt.", "error")
    return RedirectResponse(f"/watchlists/{watchlist_id}", status_code=303)
