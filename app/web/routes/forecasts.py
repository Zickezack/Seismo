"""Prognosen: what the platform forecast, logged before the outcome was known and never changed, with their outcomes
and hit rates (userstories.md, Prognoseprotokoll und Evaluation, story 1), broken down by what may explain them and over
time (story 3). Plain GET filters: bookmarkable."""
from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.services import forecast_evaluation_service, forecast_service
from app.web.templating import templates

router = APIRouter(prefix="/forecasts", tags=["forecasts"])


@router.get("")
def forecasts_page(request: Request, status: str = "", horizon: str = forecast_evaluation_service.DEFAULT_HORIZON) -> Response:
    engine = request.app.state.engine
    context = {
        "overview": forecast_service.overview(engine, status),
        "evaluation": forecast_evaluation_service.evaluation(engine, horizon),
        "horizons": forecast_evaluation_service.HORIZON_LABELS,
    }
    return templates.TemplateResponse(request, "forecasts.html", context)
