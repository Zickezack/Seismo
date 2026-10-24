"""Ereignisse pages in the web layer, without a database: the service is replaced."""
import re

import pytest
from fastapi.testclient import TestClient

from app.services import event_service
from app.services.event_service import EventDetail, EventListItem, EventOverview, ReportView
from app.web.main import create_app
from tests.unit.test_web_articles import AUTH, NOW, SETTINGS, tag_of, text_of


def event(event_id, **fields):
    values = dict(id=event_id, title=f"Earthquake <{event_id}>", event_type="natural_disaster", country="CN",
                  first_reported_at=NOW, last_reported_at=NOW, report_count=3, source_count=2)
    return EventListItem(**values | fields)


def report(candidate_id, method, score=None, **fields):
    values = dict(id=candidate_id, title=f"Report {candidate_id}", status="update", assignment_method=method,
                  assignment_score=score, article_id=candidate_id * 10, article_title=f"Artikel {candidate_id}",
                  article_url=f"https://example.org/{candidate_id}", moment=NOW, source_display_name="Xinhua")
    return ReportView(**values | fields)


@pytest.fixture
def client():
    with TestClient(create_app(SETTINGS, AUTH), follow_redirects=False) as client:
        client.post("/login", data={"username": "admin", "password": "correct horse battery"})
        yield client


@pytest.fixture
def pages(monkeypatch):
    calls = {"overview": EventOverview(events=[event(2), event(1, country=None, report_count=1, source_count=1)], total=2, waiting=0)}
    monkeypatch.setattr(event_service, "overview", lambda _engine: calls["overview"])
    monkeypatch.setattr(event_service, "detail", lambda _engine, event_id: calls.get("detail"))
    return calls


def test_the_list_shows_every_event_with_its_type_country_and_counts_linked_to_its_page(client, pages):
    html = client.get("/events").text

    assert text_of(html, "event-count").startswith("2 Ereignisse.")
    assert tag_of(html, "event-title-2") == '<a href="/events/2" data-testid="event-title-2">'
    assert text_of(html, "event-title-2") == "Earthquake &lt;2&gt;"
    assert (text_of(html, "event-reports-2"), text_of(html, "event-sources-2")) == ("3", "2")
    row = re.search(r'data-testid="event-1">(.*?)</tr>', html, re.DOTALL).group(1)
    assert "<td class=\"cell-wrap\">Naturkatastrophe</td>" in row and "<td>–</td>" in row
    assert 'aria-current="page"' in tag_of(html, "nav-events")


def test_more_events_than_shown_and_candidates_still_waiting(client, pages):
    pages["overview"] = EventOverview(events=[event(1)], total=140, waiting=12)

    count = text_of(client.get("/events").text, "event-count")

    assert count == "Angezeigt: die zuletzt berichteten 1 von 140 Ereignissen.\n    12 Ereigniskandidaten warten auf die Zuordnung."


def test_no_events_yet_says_how_they_come(client, pages):
    pages["overview"] = EventOverview(events=[], total=0, waiting=1)

    html = client.get("/events").text

    assert text_of(html, "no-events").startswith("Noch keine Ereignisse.")
    assert "1 Ereigniskandidat wartet auf die Zuordnung." in text_of(html, "event-count")


def test_the_event_page_lists_its_reports_with_their_article_and_how_each_came(client, pages):
    pages["detail"] = EventDetail(event(7), [
        report(1, "new"), report(2, "score", 0.734), report(3, "llm", 0.27, article_title=None),
    ])

    html = client.get("/events/7").text

    assert (text_of(html, "event-title"), text_of(html, "meta-id"), text_of(html, "meta-reports")) == (
        "Earthquake &lt;7&gt;", "7", "3 aus 2 Quellen"
    )
    assert tag_of(html, "report-article-2") == '<a href="/articles/20#events" data-testid="report-article-2">'
    assert [text_of(html, f"report-method-{number}") for number in (1, 2, 3)] == [
        "Neues Ereignis", "Ähnlichkeit 0,73", "LLM (Ähnlichkeit 0,27)"
    ]
    # Without a title, the article's link names it.
    assert '<span class="hint">https://example.org/3</span>' in html


def test_an_unknown_event_goes_back_to_the_list(client, pages):
    response = client.get("/events/99")

    assert (response.status_code, response.headers["location"]) == (303, "/events")
    assert text_of(client.get("/events").text, "flash") == "Ereignis nicht gefunden."
