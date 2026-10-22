"""Job status: what the queue holds, failed jobs back into the queue, a test job."""
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.services import job_service
from app.web.templating import flash, templates

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Flash messages travel in the session cookie, which must stay below the browsers' 4 KB limit.
MAX_NAMES_IN_MESSAGE = 10


@router.get("")
def job_list(request: Request, status: str | None = None) -> Response:
    overview = job_service.overview(request.app.state.engine, status)
    context = {"overview": overview, "status_labels": job_service.STATUS_LABELS}
    return templates.TemplateResponse(request, "jobs.html", context)


@router.post("/requeue")
def requeue(
    request: Request,
    job_ids: Annotated[list[int], Form(alias="job_id")] = [],
    status: Annotated[str, Form()] = "",
) -> Response:
    result = job_service.requeue_failed(request.app.state.engine, job_ids)
    if result.blocked:
        flash(request, f"Nichts eingereiht, blockiert: {_names(result.blocked)}.", "error")
    elif result.processed:
        flash(request, f"{_jobs(result.processed)} erneut eingereiht.")
    else:
        flash(request, "Keine Jobs ausgewählt.", "error")
    return _back_to_list(status)


@router.post("/test")
def enqueue_test_job(request: Request) -> Response:
    job_id = job_service.enqueue_test_job(request.app.state.engine)
    flash(request, f"Testjob #{job_id} eingereiht.")
    return _back_to_list(None)


def _back_to_list(status: str | None) -> RedirectResponse:
    status = job_service.parse_status(status)
    return RedirectResponse(f"/jobs?{urlencode({'status': status})}" if status else "/jobs", status_code=303)


def _names(names: list[str]) -> str:
    shown = ", ".join(names[:MAX_NAMES_IN_MESSAGE])
    hidden = len(names) - MAX_NAMES_IN_MESSAGE
    return f"{shown} und {hidden} weitere" if hidden > 0 else shown


def _jobs(count: int) -> str:
    return f"{count} Job" if count == 1 else f"{count} Jobs"
