"""Jinja templates and static files of the dashboard."""
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.web.access import current_admin

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"


def _admin(request: Request) -> dict:
    # Every page shows who is logged in and the logout button (templates/base.html).
    return {"admin": current_admin(request)}


templates = Jinja2Templates(directory=WEB_DIR / "templates", context_processors=[_admin])
