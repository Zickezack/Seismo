"""Form posts of the dashboard pages, and the files a form downloads."""
from fastapi import Request
from fastapi.responses import Response

from app.services.transfer_service import ExportFile


async def form_values(request: Request) -> dict[str, str]:
    """Every text field of the posted form."""
    form = await request.form()
    return {name: value for name, value in form.items() if isinstance(value, str)}


def wants_json(request: Request) -> bool:
    """static/app.js saves in place and asks for JSON; a form posted without JavaScript gets a page."""
    return "application/json" in request.headers.get("accept", "")


def download(file: ExportFile) -> Response:
    """An export file the browser saves instead of showing; the page that asked for it stays."""
    return Response(
        file.content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{file.filename}"'},
    )
