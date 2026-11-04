"""Lagen in the web layer, without a database: the services are replaced."""
import re

import pytest
from fastapi.testclient import TestClient

from app.services import (
    entity_service,
    event_context_service,
    event_correction_service,
    event_service,
    event_study_service,
    impact_service,
    instrument_service,
    situation_service,
)
from app.services.bulk import BulkResult
from app.services.dashboard_service import Change
from app.services.event_service import EventDetail
from app.services.situation_service import (
    EventSituation,
    EventSituations,
    MemberView,
    Proposed,
    SituationDetail,
    SituationItem,
    SituationOverview,
    SituationRef,
    SourceCount,
    SplitResult,
)
from app.services.event_context_service import Comparison, Uncertainty
from app.web.main import create_app
from tests.unit.test_web_articles import AUTH, NOW, SETTINGS, tag_of, text_of
from tests.unit.test_web_events import NO_IMPACTS, NO_REACTION, event


@pytest.fixture
def client():
    with TestClient(create_app(SETTINGS, AUTH), follow_redirects=False) as client:
        client.post("/login", data={"username": "admin", "password": "correct horse battery"})
        yield client


def lage(situation_id=3, **fields):
    values = dict(id=situation_id, name="Houthi attacks <Red Sea>", description="Attacks on ships.", status="confirmed", created_at=NOW,
                  decided_at=NOW, merged_into_id=None, event_count=4, automatic_count=1, proposed_count=0, last_event_at=NOW, dormant=False)
    return SituationItem(**values | fields)


def member(event_id, status="confirmed", **fields):
    values = dict(event_id=event_id, title=f"Attack {event_id}", status=status, origin="proposal", score=None, added_at=NOW,
                  decided_at=None, moved_to_id=None, first_reported_at=NOW, last_reported_at=NOW, event=event(event_id))
    return MemberView(**values | fields)


def detail(**fields):
    values = dict(situation=lage(), members=[member(5), member(6, "automatic", origin="llm", score=0.23,
                  changes=[Change("escalation", NOW)])], proposed=[], removed=[], sources=[SourceCount("Reuters", 2)],
                  merge_choices=[SituationRef(4, "Red Sea shipping")])
    return SituationDetail(**values | fields)


def test_the_list_offers_proposals_to_confirm_and_shows_lagen_active_or_dormant(client, monkeypatch):
    overview = SituationOverview(
        [Proposed(lage(7, status="proposed", name="Polish court dispute"), [member(8, "proposed")])],
        [lage(), lage(9, name="Tyrrhenian quakes", dormant=True)], rejected=1, dismissed=2,
    )
    monkeypatch.setattr(situation_service, "overview", lambda _engine: overview)

    html = client.get("/situations").text

    assert text_of(html, "name-7") == "Polish court dispute"
    assert tag_of(html, "confirm") is not None and "disabled" in tag_of(html, "confirm")
    assert text_of(html, "lage-name-3") == "Houthi attacks &lt;Red Sea&gt;"
    assert re.search(r'data-testid="lage-status-9"><span class="status status-inactive">Ruhend', html)
    assert text_of(html, "not-lagen") == "1 abgelehnt, 2 vom LLM verworfen."
    assert 'aria-current="page"' in tag_of(html, "nav-situations")


def test_confirming_proposals_says_what_it_did(client, monkeypatch):
    monkeypatch.setattr(situation_service, "confirm", lambda _engine, ids: BulkResult(len(ids), []))
    monkeypatch.setattr(situation_service, "overview", lambda _engine: SituationOverview([], [], 0, 0))

    response = client.post("/situations/confirm", data={"situation_id": ["7", "8"]})

    assert response.headers["location"] == "/situations"
    assert text_of(client.get("/situations").text, "flash") == "2 Lagen bestätigt."


def test_the_lage_page_shows_its_timeline_and_offers_the_corrections(client, monkeypatch):
    monkeypatch.setattr(situation_service, "detail", lambda _engine, _id: detail())

    html = client.get("/situations/3").text

    assert text_of(html, "member-title-5") == "Attack 5"
    assert re.search(r'data-testid="member-status-6"><span class="status status-uncertain">automatisch</span><span class="hint">LLM, Ähnlichkeit 0.23', html)
    assert text_of(html, "member-changes-6").startswith("Eskalation")
    assert tag_of(html, "split") is not None and tag_of(html, "merge-select-4") is not None
    assert 'value="Houthi attacks &lt;Red Sea&gt;"' in tag_of(html, "situation-name")
    assert tag_of(html, "describe-form").count("data-inline-save") == 1
    assert "Reuters" in re.search(r'data-testid="source-list">(.*?)</ul>', html, re.DOTALL).group(1)


def test_a_proposed_lage_is_confirmed_or_rejected_on_its_page_and_offers_no_split(client, monkeypatch):
    proposed = detail(situation=lage(status="proposed"), members=[], proposed=[member(5, "proposed")], merge_choices=[])
    monkeypatch.setattr(situation_service, "detail", lambda _engine, _id: proposed)

    html = client.get("/situations/3").text

    assert tag_of(html, "confirm-situation") is not None and tag_of(html, "split") is None
    assert text_of(html, "member-title-5") == "Attack 5"
    assert tag_of(html, "watch-form") is None


def test_a_split_leads_to_the_new_lage(client, monkeypatch):
    calls = {}
    monkeypatch.setattr(situation_service, "split", lambda _engine, sid, ids, name: calls.update(args=(sid, ids, name)) or SplitResult(1, [], 11))

    response = client.post("/situations/3/split", data={"event_id": "6", "name": "Red Sea detours"})

    assert response.headers["location"] == "/situations/11"
    assert calls["args"] == (3, [6], "Red Sea detours")


def test_renaming_answers_in_place(client, monkeypatch):
    def describe(_engine, _id, name, _description):
        if not name.strip():
            raise situation_service.InvalidSituation("Name: bitte angeben.")
        return True

    monkeypatch.setattr(situation_service, "describe", describe)
    headers = {"Accept": "application/json"}

    assert client.post("/situations/3/describe", data={"name": "Red Sea"}, headers=headers).json() == {"status": "Gespeichert"}
    assert client.post("/situations/3/describe", data={"name": " "}, headers=headers).json() == {"error": "Name: bitte angeben."}


def test_the_event_page_names_its_lagen_and_offers_the_others(client, monkeypatch):
    monkeypatch.setattr(event_service, "detail", lambda _engine, event_id: EventDetail(event(8)))
    monkeypatch.setattr(event_correction_service, "similar_events", lambda _engine, event_id: [])
    monkeypatch.setattr(event_context_service, "uncertainty_of", lambda _engine, event_ids: {each: Uncertainty() for each in event_ids})
    monkeypatch.setattr(event_context_service, "earlier_events", lambda _engine, event_id: Comparison())
    monkeypatch.setattr(event_study_service, "of_event", lambda _engine, event_id: NO_REACTION)
    monkeypatch.setattr(impact_service, "of_event", lambda _engine, event_id: NO_IMPACTS)
    monkeypatch.setattr(instrument_service, "of_entities", lambda _engine, entity_ids: [])
    monkeypatch.setattr(entity_service, "of_event", lambda _engine, event_id: [])
    monkeypatch.setattr(situation_service, "of_event", lambda _engine, _id: EventSituations(
        [EventSituation(SituationRef(3, "Houthi attacks"), "confirmed", "automatic")], [SituationRef(4, "Red Sea shipping")],
    ))

    html = client.get("/events/8").text

    assert re.search(r'data-testid="meta-situations"><a href="/situations/3">Houthi attacks</a> <span class="status status-uncertain">automatisch</span>', html)
    assert re.findall(r'<option value="(\d+)">', re.search(r'data-testid="add-situation">(.*?)</select>', html, re.DOTALL).group(1)) == ["4"]


def test_adding_an_event_goes_back_to_it(client, monkeypatch):
    monkeypatch.setattr(situation_service, "add_event", lambda _engine, sid, eid: "Houthi attacks")

    response = client.post("/situations/add", data={"situation": "3", "event": "8"})

    assert response.headers["location"] == "/events/8#situations"
