"""The start page in the web layer, without a database: the service is replaced."""
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.services import dashboard_service, event_service
from app.services.dashboard_service import Change, Dashboard, DashboardEvent
from app.web.main import create_app
from tests.unit.test_web_articles import AUTH, NOW, SETTINGS, tag_of, text_of
from tests.unit.test_web_events import CHOICES, event


@pytest.fixture
def client():
    with TestClient(create_app(SETTINGS, AUTH), follow_redirects=False) as client:
        client.post("/login", data={"username": "admin", "password": "correct horse battery"})
        yield client


@pytest.fixture
def board(monkeypatch):
    calls = {"dashboard": Dashboard("24h", NOW - timedelta(hours=24), [], [], 0, 0, 0)}

    def overview(_engine, period, criteria):
        calls["period"], calls["criteria"] = period, criteria
        return calls["dashboard"]

    monkeypatch.setattr(dashboard_service, "overview", overview)
    monkeypatch.setattr(event_service, "filter_choices", lambda _engine: CHOICES)
    return calls


def test_the_start_page_lists_new_and_developing_events_with_their_changes(client, board):
    board["dashboard"] = Dashboard(
        "3d", NOW - timedelta(days=3),
        [DashboardEvent(event(5, report_count=4, source_count=3), 4, 3)],
        [DashboardEvent(event(2, report_count=9), 2, 1, [Change("escalation", NOW)])],
        new_total=30, developing_total=1, changed_total=1,
    )

    html = client.get("/", params={"period": "3d"}).text

    assert board["period"] == "3d"
    assert 'aria-current="true"' in tag_of(html, "period-3d") and "aria-current" not in tag_of(html, "period-24h")
    assert tag_of(html, "new-title-5") == '<a href="/events/5" data-testid="new-title-5">'
    assert text_of(html, "new-recent-5") == "4 Berichte aus 3 Quellen"
    assert re.search(r'data-testid="developing-recent-2">2 Berichte aus 1 Quelle<span class="hint">von 9 insgesamt</span>', html)
    assert text_of(html, "developing-change-2-1") == "Eskalation"
    assert text_of(html, "new-more").startswith("Angezeigt: 1 von 30")
    summary = re.search(r'data-testid="dashboard-summary">(.*?)</p>', html, re.DOTALL).group(1)
    assert "30 neue Ereignisse, 1 in Entwicklung, davon 1 mit Zustandswechsel" in summary
    assert 'aria-current="page"' in tag_of(html, "nav-dashboard")


def test_a_quiet_period_says_so(client, board):
    html = client.get("/").text

    assert text_of(html, "no-new") == "Kein neues Ereignis in den letzten 24 Stunden."
    assert text_of(html, "no-developing") == "Kein früher berichtetes Ereignis kam in den letzten 24 Stunden wieder vor."
    assert board["period"] == ""


def test_the_start_page_passes_the_filter_and_keeps_it_in_its_links(client, board):
    board["dashboard"] = Dashboard("3d", NOW - timedelta(days=3), [DashboardEvent(event(5), 1, 1)], [], 30, 0, 0)

    html = client.get("/", params={"period": "3d", "type": "labor", "sources": "2", "development": "new"}).text

    criteria = board["criteria"]
    # The start page has its own period and splits new from developing itself.
    assert (criteria.event_type, criteria.min_sources, criteria.since, criteria.development) == ("labor", 2, None, None)
    assert board["period"] == "3d"
    assert "open" in tag_of(html, "section-filter")
    assert tag_of(html, "filter-period") is None and '<input type="hidden" name="period" value="3d">' in html
    assert 'href="/?type=labor&amp;sources=2&amp;period=7d"' in tag_of(html, "period-7d")
    assert 'href="/events?type=labor&amp;sources=2&amp;period=3d&amp;development=new"' in tag_of(html, "new-all")
    assert tag_of(html, "reset-filter") == '<a class="button button-secondary" href="/?period=3d" data-testid="reset-filter">'
    assert "die zum Filter passen" in re.search(r'data-testid="dashboard-summary">(.*?)</p>', html, re.DOTALL).group(1)


def test_without_a_filter_it_stays_folded(client, board):
    html = client.get("/").text

    assert "open" not in tag_of(html, "section-filter")
    assert tag_of(html, "period-7d") == '<a class="filter" href="/?period=7d" data-testid="period-7d">'
