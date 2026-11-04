"""Backtests of the Wirkungsannahmen (userstories.md, Prognoseprotokoll und Evaluation, story 2): a run asked for here
is computed by the worker behind live work and stored for good; its page judges it on the test period only."""
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from app.services import backtest_service
from app.services.backtest_service import InvalidBacktest
from app.web.templating import flash, templates

router = APIRouter(prefix="/backtests", tags=["backtests"])


@router.get("")
def backtests_page(request: Request) -> Response:
    return _page(request)


def _page(request: Request, form: dict | None = None, error: str | None = None) -> Response:
    overview = backtest_service.overview(request.app.state.engine)
    context = {
        "overview": overview, "form": form or {}, "error": error, "modes": backtest_service.MODE_LABELS,
        "min_probabilities": backtest_service.MIN_PROBABILITY_LABELS, "horizons": (5, 20, 60),
    }
    return templates.TemplateResponse(request, "backtests.html", context)


@router.post("")
async def start(request: Request) -> Response:
    form = {key: str(value) for key, value in (await request.form()).items()}
    try:
        backtest_service.start(request.app.state.engine, form)
    except InvalidBacktest as error:
        return _page(request, form, str(error))
    flash(request, "Backtest angefordert: der Worker rechnet ihn nach der laufenden Arbeit.")
    return RedirectResponse("/backtests", status_code=303)


@router.get("/{run_id}")
def run_page(request: Request, run_id: int) -> Response:
    run = backtest_service.detail(request.app.state.engine, run_id)
    if run is None:
        flash(request, "Backtest-Lauf nicht gefunden.", "error")
        return RedirectResponse("/backtests", status_code=303)
    return templates.TemplateResponse(request, "backtest.html", {"run": run})


@router.post("/{run_id}/reproduce")
def reproduce(request: Request, run_id: int) -> Response:
    if backtest_service.reproduce(request.app.state.engine, run_id) is None:
        flash(request, "Backtest-Lauf nicht gefunden.", "error")
    else:
        flash(request, f"Lauf {run_id} wird mit demselben Datenstand erneut gerechnet.")
    return RedirectResponse("/backtests", status_code=303)
