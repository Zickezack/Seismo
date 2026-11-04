"""The Backtests pages in the web layer, without a database."""
import re
from datetime import UTC, date, datetime

import pytest

from app.services import backtest_service
from app.services.backtest_service import (
    FormChoices,
    InvalidBacktest,
    Overview,
    Parameters,
    PendingItem,
    RunItem,
    RunView,
    calibration,
    segments,
    threshold,
    verdict,
)
from tests.unit.test_backtest_service import trade
from tests.unit.test_web_articles import tag_of, text_of
from tests.unit.test_web_events import client  # noqa: F401

def inner(html, testid, end="</tr>"):
    """The text of the element with that test id up to `end`, its tags left out."""
    match = re.search(rf'data-testid="{testid}"[^>]*>(.*?){end}', html, re.DOTALL)
    return " ".join(re.sub(r"<[^>]+>", " ", match.group(1)).split()) if match else None


PARAMETERS = Parameters("created", 20, "low", "qwen3.5:9b", 3, 10, 50)
AS_OF = datetime(2026, 11, 4, 18, tzinfo=UTC)


def overview(runs=(), pending=()):
    return Overview(list(runs), list(pending), FormChoices([("", "Alle Signale, auch des Analysten"), ("qwen3.5:9b|3", "qwen3.5:9b, Prompt 3")], 10, 50))


def run_view(**values):
    trades = [trade(1), trade(2, status="gap", asset_return=None, benchmark_return=None, missing=[{"symbol": "SHEL", "day": "2026-11-10"}])]
    found = segments(trades, PARAMETERS.cost, True)
    rows, rising = calibration(trades, PARAMETERS.cost, True)
    base = dict(
        id=4, as_of=AS_OF, created_at=AS_OF, code_version=1, parameters=PARAMETERS, training_until=date(2026, 6, 30),
        validation_until=None, trades_hash="ab" * 32, reproduces_id=None, reproduced=None, differing=0, segments=found,
        calibration=rows, rising=rising, verdict=verdict("created", True, {each.key: each for each in found}, 2),
        warnings=["Mehrfachtest: Test-Perioden wurden in 2 Varianten ausgewertet."], variants=2, threshold=threshold(2),
        trades=[backtest_service.TradeView(**{**each.__dict__, "net": None if each.asset_return is None else 0.04 - PARAMETERS.cost}) for each in trades],
        counts={"signals": 2, "measured": 1, "open": 0, "unmeasured": 1, "paper": 0},
    )
    return RunView(**(base | values))


@pytest.fixture
def backtests(monkeypatch):
    calls = {}
    monkeypatch.setattr(backtest_service, "overview", lambda _engine: calls.get("overview") or overview())
    monkeypatch.setattr(backtest_service, "detail", lambda _engine, run_id: calls.get("detail", {}).get(run_id))

    def start(_engine, form):
        calls["form"] = form
        if form.get("horizon") == "7":
            raise InvalidBacktest("Horizont: bitte 5, 20 oder 60 Handelstage wählen.")
        return 1

    def reproduce(_engine, run_id):
        calls["reproduced"] = run_id
        return 2 if run_id == 4 else None

    monkeypatch.setattr(backtest_service, "start", start)
    monkeypatch.setattr(backtest_service, "reproduce", reproduce)
    return calls


def test_the_page_offers_a_run_and_lists_the_runs_and_those_still_computed(client, backtests):
    backtests["overview"] = overview(
        [RunItem(4, AS_OF, PARAMETERS, None, 12, 7)], [PendingItem(9, "queued", AS_OF, PARAMETERS, 4)],
    )
    html = client.get("/backtests").text

    assert tag_of(html, "nav-backtests") is not None
    assert 'value="10"' in tag_of(html, "backtest-cost") and 'value="50"' in tag_of(html, "backtest-borrow")
    assert '<option value="qwen3.5:9b|3">qwen3.5:9b, Prompt 3</option>' in html
    assert inner(html, "pending-9", "</li>").startswith("Wartet: Ab Entstehung · 20 Handelstage · alle Stufen · qwen3.5:9b, Prompt 3 · Wiederholung von Lauf 4")
    run = inner(html, "run-4")
    assert "7 von 12 gemessen" in run and "10 bp je Seite und Bein" in run
    assert tag_of(html, "no-runs") is None


def test_starting_a_run_redirects_and_an_invalid_form_keeps_the_input(client, backtests):
    response = client.post("/backtests", data={"mode": "event_day", "horizon": "5"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/backtests"
    assert backtests["form"] == {"mode": "event_day", "horizon": "5"}

    html = client.post("/backtests", data={"mode": "event_day", "horizon": "7", "backtest_cost_bps": "25"}).text
    assert text_of(html, "backtest-error") == "Horizont: bitte 5, 20 oder 60 Handelstage wählen."
    assert '<option value="event_day" selected>' in html and 'value="25"' in tag_of(html, "backtest-cost")


def test_a_runs_page_shows_its_parameters_warnings_verdict_figures_and_trades(client, backtests):
    backtests["detail"] = {4: run_view()}
    html = client.get("/backtests/4").text

    assert text_of(html, "run-periods") == "Training bis 30.06.2026, danach Test"
    assert text_of(html, "run-warning-1").startswith("Mehrfachtest")
    assert tag_of(html, "run-verdict") is not None and 'data-state="too_few"' in tag_of(html, "run-verdict")
    assert text_of(html, "run-trading").startswith("Echter Handel: keiner. Nicht bewährt")
    assert "1 von 1 (100 %)" in inner(html, "figures-all")
    assert text_of(html, "baseline-all-long").startswith("-")
    assert inner(html, "trade-outcome-2", "</td>") == "nicht messbar: Lücke ohne Kurs: SHEL 10.11.2026"
    assert tag_of(html, "run-reproduced") is None


def test_a_run_again_says_whether_it_came_out_the_same(client, backtests):
    backtests["detail"] = {5: run_view(id=5, reproduces_id=4, reproduced=False, differing=3)}
    assert inner(client.get("/backtests/5").text, "run-reproduced", "</td>") == "von Lauf 4 : abweichend, 3 Trades anders"

    response = client.post("/backtests/4/reproduce", follow_redirects=False)
    assert response.status_code == 303 and backtests["reproduced"] == 4


def test_an_unknown_run_goes_back_to_the_list(client, backtests):
    response = client.get("/backtests/8", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/backtests"
    assert client.post("/backtests/8/reproduce", follow_redirects=False).status_code == 303
