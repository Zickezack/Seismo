"""Form posts of the dashboard pages."""
from fastapi import Request


async def form_values(request: Request) -> dict[str, str]:
    """Every text field of the posted form."""
    form = await request.form()
    return {name: value for name, value in form.items() if isinstance(value, str)}


def wants_json(request: Request) -> bool:
    """static/app.js saves in place and asks for JSON; a form posted without JavaScript gets a page."""
    return "application/json" in request.headers.get("accept", "")
