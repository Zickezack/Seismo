"""Start page after the login: the events new and developing within a period (dashboard_service)."""
from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.services import dashboard_service
from app.web.templating import templates

router = APIRouter(tags=["dashboard"])


@router.get("/")
def start_page(request: Request, period: str = "") -> Response:
    """The period by a plain link: it works without JavaScript and the URL can be bookmarked."""
    context = {
        "dashboard": dashboard_service.overview(request.app.state.engine, period),
        "periods": {key: label for key, (label, _) in dashboard_service.PERIODS.items()},
    }
    return templates.TemplateResponse(request, "dashboard.html", context)
