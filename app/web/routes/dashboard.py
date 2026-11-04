"""Start page after the login: the events new and developing within a period that match the filter
(dashboard_service)."""
from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.services import dashboard_service, event_service, opportunity_service
from app.web.templating import templates

router = APIRouter(tags=["dashboard"])
# The start page shows the newest Chancen; the page Chancen holds the rest.
DASHBOARD_CHANCES = 5


@router.get("/")
def start_page(request: Request, period: str = "") -> Response:
    """The period by a plain link, the filter by a plain GET form: both work without JavaScript and the URL can be
    bookmarked."""
    engine = request.app.state.engine
    form = event_service.parse_filter(request.query_params, with_period=False)
    dashboard = dashboard_service.overview(engine, period, criteria=form.criteria)
    chances, total, _, _ = opportunity_service.opportunities(engine, dashboard.since, limit=DASHBOARD_CHANCES)
    context = {
        "dashboard": dashboard,
        "chances": chances,
        "chances_total": total,
        "periods": {key: label for key, (label, _) in dashboard_service.PERIODS.items()},
        "filter": form,
        "choices": event_service.filter_choices(engine),
    }
    return templates.TemplateResponse(request, "dashboard.html", context)
