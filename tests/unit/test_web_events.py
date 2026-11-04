"""Ereignisse pages in the web layer, without a database: the services are replaced."""
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.repositories.event_repository import EventFilter
from app.services import (
    entity_service,
    event_context_service,
    event_correction_service,
    event_service,
    event_study_service,
    impact_service,
    instrument_service,
)
from app.services.bulk import BulkResult
from app.services.entity_service import EntityRef
from app.services.event_correction_service import InvalidReason, SimilarEvent, SplitResult
from app.services.event_service import (
    CorrectionView,
    EventDetail,
    EventListItem,
    EventOverview,
    EventRef,
    FilterChoices,
    ReportView,
    StateEffect,
    StateView,
)
from app.services.extraction_service import StatementView
from app.services.event_context_service import (
    Comparison,
    EarlierEvent,
    ImpactConflict,
    Levels,
    Outcome,
    ReportRef,
    UncertainForm,
    Uncertainty,
)
from app.services.event_study_service import (
    WINDOWS,
    Adjustment,
    Benchmark,
    EventReaction,
    Measurement,
    Model,
    Period,
    Reaction,
    SectorRef,
    Status,
    WindowResult,
)
from app.services.impact_service import EventImpacts
from app.web.main import create_app
from tests.unit.test_web_articles import AUTH, NOW, SETTINGS, tag_of, text_of


NO_REACTION = EventReaction(None, None, Period.UNDIVIDED, None, [], [])
NO_IMPACTS = EventImpacts([], None)
CHOICES = FilterChoices(["CH", "US"], [EntityRef(7, "Halbleiter"), EntityRef(8, "Öl und Gas")])


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
    monkeypatch.setattr(event_service, "overview", lambda _engine, criteria: calls.update(criteria=criteria) or calls["overview"])
    monkeypatch.setattr(event_service, "filter_choices", lambda _engine: calls.get("choices", CHOICES))
    monkeypatch.setattr(event_service, "detail", lambda _engine, event_id: calls.get("detail"))
    monkeypatch.setattr(event_correction_service, "similar_events", lambda _engine, event_id: calls.get("similar", []))
    monkeypatch.setattr(event_context_service, "uncertainty_of", lambda _engine, event_ids: calls.get("uncertainty") or {each: Uncertainty() for each in event_ids})
    monkeypatch.setattr(event_context_service, "earlier_events", lambda _engine, event_id: calls.get("earlier", Comparison()))
    monkeypatch.setattr(entity_service, "of_event", lambda _engine, event_id: calls.get("entities", []))
    monkeypatch.setattr(instrument_service, "of_entities", lambda _engine, entity_ids: calls.get("instruments", []))
    monkeypatch.setattr(event_study_service, "of_event", lambda _engine, event_id: calls.get("reaction", NO_REACTION))
    monkeypatch.setattr(impact_service, "of_event", lambda _engine, event_id: NO_IMPACTS)
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


def test_the_chronicle_traces_every_statement_of_every_report_to_its_quote_and_article(client, pages):
    later = NOW + timedelta(hours=2)
    pages["detail"] = EventDetail(
        event(7), [report(1, "new"), report(2, "score", 0.81, moment=later, source_display_name="AP", effect=StateEffect.CONFIRMS)],
        statements={
            1: [StatementView("fact", "The quake hit at 9:05.", "Das Beben ereignete sich um 9.05 Uhr", "high", "high")],
            2: [StatementView("claim", "Officials expect more victims.", "Behörden rechnen mit weiteren Opfern", "medium", None)],
        },
    )

    html = client.get("/events/7").text

    assert re.findall(r'data-testid="chronicle-report-(\d+)"', html) == ["1", "2"]
    assert tag_of(html, "chronicle-article-2") == '<a href="/articles/20#events" data-testid="chronicle-article-2">'
    first = re.search(r'data-testid="chronicle-statements-1">(.*?)</ul>', html, re.DOTALL).group(1)
    assert "Tatsache" in first and "The quake hit at 9:05." in first and "Extraktion hoch · Quelle hoch" in first
    assert '<blockquote class="quote">Das Beben ereignete sich um 9.05 Uhr</blockquote>' in first
    second = re.search(r'data-testid="chronicle-report-2">(.*?)</li>\s*</ol>', html, re.DOTALL).group(1)
    assert "AP" in second and "bestätigt" in second and "Quelle unbekannt" in second
    # Two sources, the one that reported first first.
    assert [re.search(rf'data-testid="source-{index}">\s*<td class="cell-title">([^<]*)<', html).group(1) for index in (1, 2)] == ["Xinhua", "AP"]
    assert re.findall(r'<a href="#(\w+)" data-testid="section-nav-\w+">', html)[:6] == ["states", "uncertainty", "chronicle", "reports", "sources", "entities"]


def test_the_list_passes_the_filter_of_the_url_and_shows_it_again(client, pages):
    pages["overview"] = EventOverview(events=[event(1)], total=140, waiting=0)

    html = client.get("/events", params={"country": "US", "sector": "8", "company": "Apple", "sources": "3", "impact": "1",
                                         "horizon": "weeks", "type": "labor", "development": "new", "period": "7d"}).text

    criteria = pages["criteria"]
    assert (criteria.country, criteria.sector_id, criteria.company, criteria.min_sources) == ("US", 8, "Apple", 3)
    assert (criteria.horizon, criteria.event_type, criteria.development) == ("weeks", "labor", "new") and criteria.since is not None
    assert "open" in tag_of(html, "section-filter")
    assert text_of(html, "event-count").startswith("Angezeigt: die zuletzt berichteten 1 von 140 passenden Ereignissen.")
    for testid, value in (("filter-period", "7d"), ("filter-development", "new"), ("filter-type", "labor"), ("filter-country", "US"),
                          ("filter-sector", "8"), ("filter-sources", "3"), ("filter-horizon", "weeks")):
        select = re.search(rf'data-testid="{testid}">(.*?)</select>', html, re.DOTALL).group(1)
        assert re.findall(r'<option value="([^"]*)" selected>', select) == [value], testid
    assert 'value="Apple"' in tag_of(html, "filter-company")
    assert "checked" in tag_of(html, "filter-impact")
    assert tag_of(html, "reset-filter") == '<a class="button button-secondary" href="/events" data-testid="reset-filter">'


def test_the_filter_offers_the_countries_and_sectors_known(client, pages):
    html = client.get("/events").text

    country = re.search(r'data-testid="filter-country">(.*?)</select>', html, re.DOTALL).group(1)
    assert re.findall(r'<option value="([^"]*)"', country) == ["", "CH", "US"]
    sector = re.search(r'data-testid="filter-sector">(.*?)</select>', html, re.DOTALL).group(1)
    assert re.findall(r'<option value="([^"]*)">([^<]*)<', sector) == [("", "Alle Branchen"), ("7", "Halbleiter"), ("8", "Öl und Gas")]
    assert pages["criteria"] == EventFilter() and tag_of(html, "reset-filter") is None


def test_no_event_matching_the_filter_says_so(client, pages):
    pages["overview"] = EventOverview(events=[], total=0, waiting=0)

    html = client.get("/events", params={"type": "labor"}).text

    assert text_of(html, "no-matching-events") == "Kein Ereignis passt zu diesem Filter."
    assert text_of(html, "no-events") is None


@pytest.mark.parametrize(("total", "expected"), [(1, "1 passendes Ereignis."), (3, "3 passende Ereignisse.")])
def test_the_count_of_a_filtered_list_says_how_many_match(client, pages, total, expected):
    pages["overview"] = EventOverview(events=[event(each) for each in range(total)], total=total, waiting=0)

    assert text_of(client.get("/events", params={"period": "24h"}).text, "event-count") == expected


# Historical search, uncertainty and comparable earlier events (Dashboard, Recherche und Alerts, story 4).

def uncertain(**fields):
    values = dict(
        reports=3, statements=5, kinds={"fact": 3, "claim": 2}, extraction=Levels(3, 1, 1, 0), source_quality=Levels(0, 4, 0, 1),
        inference=Levels(1, 0, 1, 1), contradicting=[ReportRef(2, "Report <2>")], llm_assigned=1,
        impact_conflicts=[ImpactConflict("Maersk", 4, [ReportRef(1, "Report 1")], [ReportRef(2, "Report <2>")])],
        uncertain_forms=[UncertainForm("Maersk Line", 4, "A.P. Moller-Maersk")],
    )
    return Uncertainty(**values | fields)


def test_the_list_searches_a_period_of_the_past_and_shows_it_again(client, pages):
    html = client.get("/events", params={"q": " port  strike ", "from": "2025-11-03", "to": "2025-11-05", "type": "labor"}).text

    criteria = pages["criteria"]
    assert criteria.text == "port strike" and criteria.until - criteria.since == timedelta(days=3)
    assert 'value="port strike"' in tag_of(html, "filter-q")
    assert 'value="2025-11-03"' in tag_of(html, "filter-from") and 'value="2025-11-05"' in tag_of(html, "filter-to")
    # A rule keeps the type, neither search nor period.
    assert tag_of(html, "save-as-rule") == '<a class="button button-secondary" href="/alerts/rules/new?type=labor" data-testid="save-as-rule">'


def test_a_search_alone_offers_no_alert_rule(client, pages):
    html = client.get("/events", params={"q": "strike"}).text
    assert tag_of(html, "save-as-rule") is None and tag_of(html, "reset-filter") is not None


def test_each_listed_event_shows_its_evidence_and_uncertainty_never_one_score(client, pages):
    pages["uncertainty"] = {2: uncertain(), 1: Uncertainty()}

    html = client.get("/events").text

    assert text_of(html, "event-uncertainty-2-kinds") == "3 Tatsachen, 2 Behauptungen"
    assert text_of(html, "event-uncertainty-2-contradicting") == "1 widerspricht dem Zustand"
    assert text_of(html, "event-uncertainty-2-impacts") == "1 Wirkung gegensätzlich"
    assert text_of(html, "event-uncertainty-2-weak") == "Niedrig oder unbekannt: Extraktion 1/5 · Quelle 1/5 · Schlussfolgerung 2/3"
    assert "1 vom LLM zugeordnet · 1 unsichere Namensform" in html
    assert text_of(html, "event-uncertainty-1-kinds") == "keine Aussagen"
    assert tag_of(html, "event-uncertainty-1-contradicting") is None


def test_the_event_page_shows_uncertainty_and_contradictions_in_full(client, pages):
    pages["detail"] = EventDetail(event(7), [report(1, "new", founding=True), report(2, "score", 0.6)])
    pages["uncertainty"] = {7: uncertain()}

    html = client.get("/events/7").text

    assert text_of(html, "uncertainty-kinds") == "3 Tatsachen, 2 Behauptungen"
    assert text_of(html, "uncertainty-extraction") == "3 hoch · 1 mittel · 1 niedrig"
    assert text_of(html, "uncertainty-source") == "4 mittel · 1 unbekannt"
    assert text_of(html, "uncertainty-inference") == "1 hoch · 1 niedrig · 1 unbekannt"
    assert text_of(html, "uncertainty-llm") == "1 von 3 Berichten vom LLM zugeordnet"
    contradicting = re.search(r'data-testid="uncertainty-contradicting">(.*?)</ul>', html, re.DOTALL).group(1)
    assert '<a href="#chronicle-2">Report &lt;2&gt;</a>' in contradicting
    impacts = re.search(r'data-testid="uncertainty-impacts">(.*?)</table>', html, re.DOTALL).group(1)
    assert '<a href="/entities/4">Maersk</a>' in impacts and '<a href="#chronicle-1">Report 1</a>' in impacts
    assert "„Maersk Line“ als <a href=\"/entities/4\">A.P. Moller-Maersk</a>" in html
    assert 'id="chronicle-2"' in html
    assert tag_of(html, "uncertainty-no-contradictions") is None


def test_an_event_without_contradictions_says_so(client, pages):
    pages["detail"] = EventDetail(event(7), [report(1, "new", founding=True)])
    pages["uncertainty"] = {7: uncertain(contradicting=[], impact_conflicts=[], uncertain_forms=[])}

    html = client.get("/events/7").text

    assert tag_of(html, "uncertainty-no-contradictions") is not None
    assert tag_of(html, "uncertainty-contradicting") is None and tag_of(html, "uncertainty-forms") is None


def test_the_event_page_lists_comparable_earlier_events_with_why_and_how_they_went_on(client, pages):
    pages["detail"] = EventDetail(event(7), [report(1, "new", founding=True)])
    earlier = EarlierEvent(
        event(3, title="Quake <old>", state="closure", first_reported_at=NOW - timedelta(days=400), last_reported_at=NOW - timedelta(days=396)),
        0.31, ["Naturkatastrophe"], ["CN"], [EntityRef(5, "China Earthquake Networks Center")],
        Outcome("closure", True, NOW - timedelta(days=397), timedelta(days=4)), uncertain(),
    )
    other = EarlierEvent(event(4), 0.12, [], [], [], Outcome("update", False, None, timedelta(minutes=5)), Uncertainty())
    pages["earlier"] = Comparison([earlier, other])

    html = client.get("/events/7").text

    assert text_of(html, "earlier-title-3") == "Quake &lt;old&gt;"
    assert text_of(html, "earlier-summary") == "Davon 1 abgeschlossen, 1 andauernd; 1 ist eskaliert."
    reasons = re.search(r'data-testid="earlier-reasons-3">(.*?)</td>', html, re.DOTALL).group(1)
    assert "Ähnlichkeit 0,31" in reasons and "Typ: Naturkatastrophe" in reasons and "Land: CN" in reasons
    assert '<a href="/entities/5">China Earthquake Networks Center</a>' in reasons
    other_reasons = re.search(r'data-testid="earlier-reasons-4">(.*?)</td>', html, re.DOTALL).group(1)
    assert "Typ: ein anderer" in other_reasons and "Land: kein gemeinsames" in other_reasons and "Entitäten: keine" in other_reasons
    outcome = re.search(r'data-testid="earlier-outcome-3">(.*?)</td>', html, re.DOTALL).group(1)
    assert "Abschluss, eskaliert" in outcome and "abgeschlossen seit" in outcome and "berichtet über 4 Tage" in outcome
    assert text_of(html, "earlier-uncertainty-3-contradicting") == "1 widerspricht dem Zustand"
    assert tag_of(html, "section-nav-earlier") is not None


def test_without_comparable_earlier_events_the_page_says_so(client, pages):
    pages["detail"] = EventDetail(event(7), [report(1, "new", founding=True)])

    html = client.get("/events/7").text

    assert text_of(html, "no-earlier") == "Kein früheres Ereignis ist vergleichbar."


def test_the_event_page_shows_the_market_reaction_with_both_measures_gaps_and_adjustments(client, pages):
    pages["detail"] = EventDetail(event(7), [report(1, "new", founding=True)])
    day, d5 = date(2025, 6, 10), date(2025, 6, 17)
    results = [
        WindowResult(WINDOWS[1], day, day, Status.DONE, [], 0.062, 0.004, 0.058, 0.0512, 4.24),
        WindowResult(WINDOWS[3], day, d5, Status.GAP, [("XCO", date(2025, 6, 13))]),
        WindowResult(WINDOWS[5], day, date(2025, 9, 4), Status.OPEN, [], cut=True),
    ]
    market = Measurement(Benchmark.MARKET, "SPY", "SPDR S&P 500", Model(0.0002, 1.53, 0.004, 120), results)
    sector = Measurement(Benchmark.SECTOR, "XLE", "Energy Select", None, results[:1])
    pages["reaction"] = EventReaction(
        day, (date(2024, 12, 9), date(2025, 5, 23)), Period.TRAINING, SectorRef("oil-gas", "Öl und Gas", "XLE"),
        [Reaction(3, "XCO", "Exco Inc.", [market, sector], [Adjustment("XCO", date(2025, 3, 18), "Aktiensplit 1:2")], [date(2025, 6, 13)])],
        [],
    )

    html = client.get("/events/7").text

    assert text_of(html, "reaction-period") == "Training"
    assert text_of(html, "reaction-sector") == "Öl und Gas (XLE)"
    assert "Ereignistag 10.06.2025" in html and "Schätzfenster 09.12.2024 bis 23.05.2025" in html
    assert text_of(html, "excess-3-market-day") == "+5,80 %"
    assert text_of(html, "abnormal-3-market-day") == "+5,12 %"
    assert "Alpha +0,02 % pro Tag, Beta 1,53" in text_of(html, "reaction-model-3-market")
    assert "zu wenig Daten" in text_of(html, "reaction-model-3-sector")
    gap = re.search(r'data-testid="status-3-market-d5">(.*?)</td>', html, re.DOTALL).group(1)
    assert gap.startswith("Lücke") and "ohne Kurs: XCO 13.06.2025" in gap
    open_window = re.search(r'data-testid="status-3-market-d60">(.*?)</td>', html, re.DOTALL).group(1)
    assert open_window.startswith("offen bis 04.09.2025") and "zählt dort nicht" in open_window
    assert text_of(html, "reaction-adjustments-3") == "Anpassungen: XCO Aktiensplit 1:2 am 18.03.2025."
    assert text_of(html, "reaction-gaps-3") == "Lücken von XCO im ganzen Zeitraum: 13.06.2025."
    assert tag_of(html, "section-nav-reaction") is not None


def test_without_a_us_wertpapier_the_page_says_there_is_no_reaction(client, pages):
    pages["detail"] = EventDetail(event(7), [report(1, "new", founding=True)])

    html = client.get("/events/7").text

    assert text_of(html, "no-reaction") == "Keine Entität seiner Berichte hat ein US-Wertpapier mit Tageskursen (Alpaca)."
    assert text_of(html, "reaction-sector") == "keine genannt, nur gegen den Markt"
