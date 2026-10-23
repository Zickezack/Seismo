"""Artikel: the most recently fetched, all or of one source. The research dashboard follows with its epic."""
from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.services import article_service
from app.web.templating import templates

router = APIRouter(prefix="/articles", tags=["articles"])


@router.get("")
def article_list(request: Request, source: str | None = None) -> Response:
    context = {"overview": article_service.overview(request.app.state.engine, source)}
    return templates.TemplateResponse(request, "articles.html", context)
