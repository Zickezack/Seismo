"""Artikel: the most recently fetched, all or of one source; one article with its duplicates and similar articles,
where the admin separates wrong ones. The research dashboard follows with its epic."""
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.services import article_service, extraction_service, similarity_service
from app.web.templating import flash, name_list, templates

router = APIRouter(prefix="/articles", tags=["articles"])

NOT_FOUND = "Artikel nicht gefunden."
ArticleIds = Annotated[list[int], Form(alias="article_id")]


@router.get("")
def article_list(request: Request, source: str | None = None, hide_duplicates: str | None = None) -> Response:
    # A checkbox: the browser sends it only when it is ticked.
    context = {"overview": article_service.overview(request.app.state.engine, source, bool(hide_duplicates))}
    return templates.TemplateResponse(request, "articles.html", context)


@router.get("/{article_id}")
def article_page(request: Request, article_id: int) -> Response:
    detail = article_service.detail(request.app.state.engine, article_id)
    if detail is None:
        flash(request, NOT_FOUND, "error")
        return RedirectResponse("/articles", status_code=303)
    extraction = extraction_service.latest_for_article(request.app.state.engine, article_id)
    return templates.TemplateResponse(request, "article.html", {"detail": detail, "extraction": extraction})


@router.post("/{article_id}/separate")
def separate_similar(request: Request, article_id: int, other_ids: ArticleIds = []) -> Response:
    """Verknüpfung aufheben: the selected similar articles are unrelated to this one."""
    result = similarity_service.separate(request.app.state.engine, article_id, other_ids)
    if result.blocked:
        flash(request, f"Nichts aufgehoben, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{result.processed} {'Verknüpfung' if result.processed == 1 else 'Verknüpfungen'} aufgehoben.")
    else:
        flash(request, "Keine Artikel ausgewählt.", "error")
    return RedirectResponse(f"/articles/{article_id}#similar", status_code=303)


@router.post("/{article_id}/not-duplicate")
def not_duplicate(request: Request, article_id: int, duplicate_ids: ArticleIds = []) -> Response:
    """Kein Duplikat: the selected duplicates - of this original, or this article itself - are no copies."""
    result = article_service.not_duplicate(request.app.state.engine, duplicate_ids)
    if result.blocked:
        flash(request, f"Nichts getrennt, blockiert: {name_list(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{result.processed} {'Duplikat' if result.processed == 1 else 'Duplikate'} getrennt.")
    else:
        flash(request, "Keine Artikel ausgewählt.", "error")
    return RedirectResponse(f"/articles/{article_id}", status_code=303)
