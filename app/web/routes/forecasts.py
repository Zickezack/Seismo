"""Prognosen: what the platform forecast, logged before the outcome was known and never changed, with their outcomes
and hit rates (userstories.md, Prognoseprotokoll und Evaluation, story 1). A plain GET filter: bookmarkable."""
from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.services import forecast_service
from app.web.templating import templates

router = APIRouter(prefix="/forecasts", tags=["forecasts"])


@router.get("")
def forecasts_page(request: Request, status: str = "") -> Response:
    context = {"overview": forecast_service.overview(request.app.state.engine, status)}
    return templates.TemplateResponse(request, "forecasts.html", context)
