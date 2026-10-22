"""Jinja templates and static files of the dashboard."""
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.web.access import current_admin

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"
FLASH_KEY = "flash"


def flash(request: Request, text: str, kind: str = "success") -> None:
    """A message for the next page shown, typically after a redirect (templates/base.html)."""
    request.session[FLASH_KEY] = {"text": text, "kind": kind}


def _admin(request: Request) -> dict:
    # Every page shows who is logged in and the logout button (templates/base.html).
    return {"admin": current_admin(request)}


def _flash(request: Request) -> dict:
    return {"flash": request.session.pop(FLASH_KEY, None)}


def _local_time(value: datetime | None) -> Markup | str:
    """A timestamp that static/app.js shows in the viewer's local time; without JavaScript it stays in UTC, as stored."""
    if value is None:
        return "–"
    utc = value.astimezone(UTC)
    return Markup('<time datetime="{}" data-local-time>{} UTC</time>').format(
        utc.isoformat(), utc.strftime("%Y-%m-%d %H:%M:%S")
    )


templates = Jinja2Templates(directory=WEB_DIR / "templates", context_processors=[_admin, _flash])
templates.env.filters["local_time"] = _local_time
