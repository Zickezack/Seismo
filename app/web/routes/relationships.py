"""Beziehungen: one relationship between two entities with every Angabe about it, and the analyst's own."""
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.services import relationship_service
from app.services.relationship_service import InvalidRelationship
from app.web.templating import flash, templates

router = APIRouter(prefix="/relationships", tags=["relationships"])

Text = Annotated[str, Form()]


@router.get("/{relationship_id}")
def relationship_page(request: Request, relationship_id: int) -> Response:
    detail = relationship_service.detail(request.app.state.engine, relationship_id)
    if detail is None:
        flash(request, "Beziehung nicht gefunden.", "error")
        return RedirectResponse("/entities", status_code=303)
    context = {"detail": detail, "reviews": relationship_service.REVIEW_LABELS}
    return templates.TemplateResponse(request, "relationship.html", context)


@router.post("/{relationship_id}/state")
def set_state(
    request: Request, relationship_id: int, valid_from: Text = "", valid_to: Text = "", review: Text = "", note: Text = ""
) -> Response:
    """Stand ändern: the analyst's Angabe of validity and review."""
    try:
        relationship_service.set_state(request.app.state.engine, relationship_id, valid_from, valid_to, review, note)
    except InvalidRelationship as error:
        flash(request, f"Nichts geändert. {error}", "error")
    else:
        flash(request, "Stand erfasst.")
    return RedirectResponse(f"/relationships/{relationship_id}#records", status_code=303)
