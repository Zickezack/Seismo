"""Event-Study: the market reactions of all events' Wertpapiere, per event type, window and period (userstories.md,
Historische Markt- und Ereignisdaten, story 2). A plain GET form chooses benchmark and measure: bookmarkable."""
from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.services import event_study_service
from app.web.templating import templates

router = APIRouter(prefix="/event-study", tags=["event-study"])


@router.get("")
def event_study_page(request: Request) -> Response:
    benchmark, measure = event_study_service.parse_choice(request.query_params)
    context = {
        "overview": event_study_service.overview(request.app.state.engine, benchmark, measure),
        "benchmark_labels": event_study_service.BENCHMARK_LABELS,
        "measure_labels": event_study_service.MEASURE_LABELS,
        "windows": event_study_service.WINDOWS,
        "estimation": event_study_service.ESTIMATION,
        "min_estimation_days": event_study_service.MIN_ESTIMATION_DAYS,
    }
    return templates.TemplateResponse(request, "event_study.html", context)
