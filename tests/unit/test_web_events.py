"""Ereignisse pages in the web layer, without a database: the services are replaced."""
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.services import entity_service, event_correction_service, event_service
from app.services.bulk import BulkResult
from app.services.event_correction_service import InvalidReason, SimilarEvent, SplitResult
from app.services.event_service import (
    CorrectionView,
    EventDetail,
    EventListItem,
    EventOverview,
    EventRef,
    ReportView,
    StateEffect,
    StateView,
)
from app.web.main import create_app
from tests.unit.test_web_articles import AUTH, NOW, SETTINGS, tag_of, text_of


def event(event_id, **fields):
    values = dict(id=event_id, title=f"Earthquake <{event_id}>", event_type="natural_disaster", country="CN",
                  first_reported_at=NOW, last_reported_at=NOW, state="update", report_count=3, source_count=2)
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
    monkeypatch.setattr(event_correction_service, "similar_events", lambda _engine, event_id: calls.get("similar", []))
    monkeypatch.setattr(entity_service, "of_event", lambda _engine, event_id: calls.get("entities", []))
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


def test_the_event_page_shows_its_versions_and_marks_a_contradicting_report(client, pages):
    later = NOW + timedelta(hours=3)
    states = [
        StateView(5, "start", NOW, NOW, 1, "Report 1", "on Friday morning", 10, "Xinhua"),
        StateView(6, "closure", later, later, None, None, None, None, None),
    ]
    pages["detail"] = EventDetail(event(7, state="closure"), [
        report(1, "new", status="start", effect=StateEffect.VERSION),
        report(2, "score", 0.734, status="resumption", effect=StateEffect.CONTRADICTS, contradicted_state="start"),
    ], states)

    html = client.get("/events/7").text

    assert text_of(html, "meta-state") == "Abschluss"
    assert text_of(html, "meta-contradictions") == "1 Bericht widerspricht"
    assert [text_of(html, f"state-status-{number}") for number in (5, 6)] == ["Beginn", "Abschluss"]
    assert tag_of(html, "state-report-5") == '<a href="/articles/10#events" data-testid="state-report-5">'
    assert "Zeitangabe im Artikel: on Friday morning" in html
    # A version whose report is gone stays, without a link.
    row = re.search(r'data-testid="state-6">(.*?)</tr>', html, re.DOTALL).group(1)
    assert "Bericht nicht mehr vorhanden" in row and tag_of(html, "state-report-6") is None
    assert text_of(html, "report-effect-1") == "Zustandswechsel"
    assert '<span class="status status-contradicts">widerspricht (Beginn)</span>' in html


def test_a_version_an_earlier_report_found_later_made_redundant_shows_as_a_confirmation(client, pages):
    earlier = NOW - timedelta(hours=1)
    states = [
        StateView(5, "update", earlier, NOW, 2, "Report 2", None, 11, "Xinhua"),
        StateView(6, "update", NOW, NOW, 1, "Report 1", None, 10, "Xinhua", repeats=True),
    ]
    pages["detail"] = EventDetail(event(7, state="update"), [report(1, "new", status="update", effect=StateEffect.CONFIRMS)], states)

    html = client.get("/events/7").text

    assert text_of(html, "state-status-5") == "Fortsetzung" and tag_of(html, "state-repeats-5") is None
    assert text_of(html, "state-repeats-6") == "bestätigt, kein Wechsel: ein später gefundener, früherer Bericht nennt den Zustand schon"
    assert text_of(html, "report-effect-1") == "bestätigt"


def test_an_event_without_a_known_state_says_so(client, pages):
    pages["detail"] = EventDetail(event(7, state=None), [report(1, "new", status="unknown")], [])

    html = client.get("/events/7").text

    assert text_of(html, "meta-state") == "–" and tag_of(html, "meta-contradictions") is None
    assert text_of(html, "no-states") == "Kein Bericht nennt bisher einen Zustand."
    assert text_of(client.get("/events").text, "event-state-2") == "Fortsetzung"


def test_an_unknown_event_goes_back_to_the_list(client, pages):
    response = client.get("/events/99")

    assert (response.status_code, response.headers["location"]) == (303, "/events")
    assert text_of(client.get("/events").text, "flash") == "Ereignis nicht gefunden."


def test_the_event_page_offers_to_split_off_reports_but_never_the_one_it_is_named_after(client, pages):
    pages["detail"] = EventDetail(event(7), [
        report(1, "new", founding=True), report(2, "score", 0.734),
    ])

    html = client.get("/events/7").text

    assert tag_of(html, "report-list").startswith('<form class="stack" method="post" action="/events/7/split" data-bulk-form')
    split = tag_of(html, "split")
    assert 'data-confirm="Bericht|Berichte"' in split and 'data-confirm-verb="abspalten"' in split and "disabled" in split
    assert 'name="reason" maxlength="1000"' in tag_of(html, "split-reason")
    assert 'name="candidate_id" value="2" data-select-row' in tag_of(html, "select-report-2")
    assert " disabled " in tag_of(html, "select-report-1") and "disabled" not in tag_of(html, "select-report-2")
    assert text_of(html, "report-founding-1") == "Nach diesem Bericht ist das Ereignis benannt."
    assert [text_of(html, f"section-nav-{name}") for name in ("states", "reports", "similar")] == [
        "Verlauf", "Berichte", "Ähnliche Ereignisse"
    ]
    assert tag_of(html, "section-nav-corrections") is None


def test_an_event_with_one_report_has_nothing_to_split(client, pages):
    pages["detail"] = EventDetail(event(7, report_count=1), [report(1, "new", founding=True)])

    html = client.get("/events/7").text

    assert tag_of(html, "split") is None and tag_of(html, "select-report-1") is None
    assert tag_of(html, "report-article-1") is not None


def test_the_event_page_lists_similar_events_to_merge_into_it(client, pages):
    pages["detail"] = EventDetail(event(7), [report(1, "new", founding=True)])
    pages["similar"] = [SimilarEvent(event(9, title="Tremor in Tibet"), 0.31), SimilarEvent(event(4, country=None), 0.05)]

    html = client.get("/events/7").text

    assert tag_of(html, "similar-list").startswith('<form class="stack" method="post" action="/events/7/merge" data-bulk-form')
    merge = tag_of(html, "merge")
    assert 'data-confirm="Ereignis|Ereignisse"' in merge and 'data-confirm-verb="zusammenführen"' in merge
    assert 'name="event_id" value="9" data-select-row data-name="Tremor in Tibet"' in tag_of(html, "select-similar-9")
    assert tag_of(html, "similar-title-9") == '<a href="/events/9" data-testid="similar-title-9">'
    assert [text_of(html, f"similar-score-{number}") for number in (9, 4)] == ["0,31", "0,05"]
    assert html.index('data-testid="similar-9"') < html.index('data-testid="similar-4"')


def test_without_similar_events_there_is_nothing_to_merge(client, pages):
    pages["detail"] = EventDetail(event(7), [report(1, "new", founding=True)])

    html = client.get("/events/7").text

    assert text_of(html, "no-similar") == "Keine ähnlichen Ereignisse in dieser Zeit."
    assert tag_of(html, "merge") is None


def correction(correction_id, kind, incoming, *others, candidate_ids=(1,), reason=None):
    refs = [EventRef(other, f"Event <{other}>") for other in others]
    return CorrectionView(correction_id, kind, NOW, reason, incoming, refs, list(candidate_ids))


def test_the_event_page_shows_its_corrections_and_what_they_did_to_its_reports_and_versions(client, pages):
    later = NOW + timedelta(hours=2)
    states = [
        StateView(5, "update", NOW, NOW, 1, "Report 1", None, 10, "Xinhua", withdrawn_by_id=31),
        StateView(6, "start", NOW, later, 2, "Report 2", None, 20, "Xinhua", added_by_id=31),
    ]
    pages["detail"] = EventDetail(event(7, state="start"), [
        report(1, "new", founding=True), report(2, "score", 0.734, moved_by="merge"),
    ], states, [
        correction(31, "merge", True, 9, 4, candidate_ids=(2, 3), reason="Same quake <Dingri>"),
        correction(32, "split", False, 12, candidate_ids=(5,)),
    ])

    html = client.get("/events/7").text

    assert text_of(html, "section-nav-corrections") == "Korrekturen"
    assert [text_of(html, f"correction-kind-{number}") for number in (31, 32)] == ["Zusammenführung", "Aufteilung"]
    merged = re.search(r'data-testid="correction-events-31">(.*?)</td>', html, re.DOTALL).group(1)
    assert merged == 'Aufgenommen: <a href="/events/9">Event &lt;9&gt;</a> (Ereignis-ID 9), <a href="/events/4">Event &lt;4&gt;</a> (Ereignis-ID 4)'
    assert re.search(r'data-testid="correction-events-32">(.*?)</td>', html).group(1).startswith('Abgespalten als <a href="/events/12">')
    assert (text_of(html, "correction-reports-31"), text_of(html, "correction-reason-31")) == ("2", "Same quake &lt;Dingri&gt;")
    assert text_of(html, "correction-reason-32") == "–"
    # A withdrawn version stays, struck through, with the correction that withdrew it.
    assert re.search(r'data-testid="state-status-5"><s>Fortsetzung</s>', html)
    assert text_of(html, "state-withdrawn-5").startswith("zurückgezogen durch Zusammenführung,")
    assert text_of(html, "state-status-6") == "Beginn" and tag_of(html, "state-withdrawn-6") is None
    assert re.search(r'data-testid="state-recorded-6">.*?<span class="hint">durch Zusammenführung</span>', html)
    assert text_of(html, "report-method-2") == "Zusammenführung"
    assert '<span class="hint">zuerst: Ähnlichkeit 0,73</span>' in html


def test_a_merged_event_says_where_it_went_and_offers_no_correction(client, pages):
    pages["detail"] = EventDetail(
        event(9, report_count=0, source_count=0), [], [], [correction(31, "merge", False, 7)], EventRef(7, "Quake <7>")
    )

    html = client.get("/events/9").text

    merged = re.search(r'data-testid="merged-into">(.*?)</p>', html).group(1)
    assert merged.startswith('Zusammengeführt in <a href="/events/7">Quake &lt;7&gt;</a> (Ereignis-ID 7).')
    assert text_of(html, "no-reports") == "Keine Berichte."
    assert tag_of(html, "similar-section") is None and tag_of(html, "split") is None
    assert re.search(r'data-testid="correction-events-31">Aufgegangen in <a href="/events/7">', html)


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (BulkResult(processed=2, blocked=[]), "2 Ereignisse in dieses zusammengeführt."),
        (BulkResult(processed=1, blocked=[]), "1 Ereignis in dieses zusammengeführt."),
        (BulkResult(processed=0, blocked=["Quake (bereits zusammengeführt)"]), "Nichts zusammengeführt, blockiert: Quake (bereits zusammengeführt)."),
        (BulkResult(processed=0, blocked=[]), "Keine Ereignisse ausgewählt."),
    ],
)
def test_merging_passes_the_selection_and_the_reason_and_reports_the_result(client, monkeypatch, pages, result, message):
    calls = []
    monkeypatch.setattr(event_correction_service, "merge", lambda _engine, *args: calls.append(args) or result)

    response = client.post("/events/7/merge", data={"event_id": ["9", "4"], "reason": "Same quake"})

    assert calls == [(7, [9, 4], "Same quake")]
    assert (response.status_code, response.headers["location"]) == (303, "/events/7#similar")
    assert text_of(client.get("/events").text, "flash") == message


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (SplitResult(processed=2, blocked=[], event_id=12), "2 Berichte als neues Ereignis 12 abgespalten."),
        (SplitResult(processed=1, blocked=[], event_id=12), "1 Bericht als neues Ereignis 12 abgespalten."),
        (SplitResult(processed=0, blocked=["Quake (benennt das Ereignis)"]), "Nichts abgespalten, blockiert: Quake (benennt das Ereignis)."),
        (SplitResult(processed=0, blocked=[]), "Keine Berichte ausgewählt."),
    ],
)
def test_splitting_passes_the_selection_and_the_reason_and_reports_the_result(client, monkeypatch, pages, result, message):
    calls = []
    monkeypatch.setattr(event_correction_service, "split", lambda _engine, *args: calls.append(args) or result)

    response = client.post("/events/7/split", data={"candidate_id": ["3", "2"]})

    assert calls == [(7, [3, 2], "")]
    assert (response.status_code, response.headers["location"]) == (303, "/events/7#reports")
    assert text_of(client.get("/events").text, "flash") == message


def test_a_reason_too_long_corrects_nothing_and_says_why(client, monkeypatch, pages):
    def too_long(*_args):
        raise InvalidReason("Die Begründung hat 1001 Zeichen, höchstens 1000 sind möglich.")

    monkeypatch.setattr(event_correction_service, "merge", too_long)

    client.post("/events/7/merge", data={"event_id": ["9"], "reason": "x" * 1001})

    assert text_of(client.get("/events").text, "flash") == (
        "Nichts zusammengeführt. Die Begründung hat 1001 Zeichen, höchstens 1000 sind möglich."
    )
