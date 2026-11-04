"""The status page: throughput, cost per article, latency, failures, LLM calls, the queue and storage, measured
(userstories.md, Skalierung und Betriebsqualität, story 1). A plain GET choice of period: bookmarkable."""
from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.services import status_service
from app.web.templating import templates

router = APIRouter(prefix="/status", tags=["status"])


@router.get("")
def status_page(request: Request, period: str = status_service.DEFAULT_PERIOD) -> Response:
    context = {
        "status": status_service.overview(request.app.state.engine, period), "duration": status_service.duration_label,
        "number": status_service.count_label, "share": status_service.share_label,
    }
    return templates.TemplateResponse(request, "status.html", context)
