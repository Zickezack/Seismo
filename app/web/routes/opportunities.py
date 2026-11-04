"""Chancen: Wirkungsannahmen of new and developing events whose Wertpapiere have not moved much yet (userstories.md,
Dashboard, Recherche und Alerts, story 5). Period, filter and order by a plain GET: bookmarkable."""
from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.services import opportunity_service
from app.web.templating import templates

router = APIRouter(prefix="/opportunities", tags=["opportunities"])


@router.get("")
def opportunities_page(request: Request, period: str = "") -> Response:
    criteria = opportunity_service.parse(request.query_params)
    context = {
        "overview": opportunity_service.overview(request.app.state.engine, period, criteria),
        "sorts": opportunity_service.SORTS,
        "choices": {
            "direction": ("Richtung", opportunity_service.DIRECTION_CHOICES),
            "probability": ("Wahrscheinlichkeit", opportunity_service.PROBABILITY_CHOICES),
            "classification": ("Einstufung", opportunity_service.CLASSIFICATION_CHOICES),
            "horizon": ("Horizont", opportunity_service.HORIZON_CHOICES),
        },
    }
    return templates.TemplateResponse(request, "opportunities.html", context)
