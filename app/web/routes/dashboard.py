"""Start page after the login. The event overview follows with the epic "Dashboard, Recherche und Alerts"."""
from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.web.templating import templates

router = APIRouter(tags=["dashboard"])


@router.get("/")
def start_page(request: Request) -> Response:
    return templates.TemplateResponse(request, "dashboard.html")
