"""Wirkungsannahmen in the web layer, without a database: the event page's section, the reviews, one hypothesis's
page with its form and versions, a new one, and the entity page. The services are replaced."""
import re
from datetime import UTC, datetime

import pytest

from app.services import impact_service
from app.services.bulk import BulkResult
from app.services.event_service import EventDetail
from app.services.impact_service import (
    AssessmentView,
    BelegView,
    EventImpacts,
    FormChoices,
    HypothesisDetail,
    HypothesisView,
    InvalidHypothesis,
    TargetChoice,
    VersionView,
    WayChoice,
)
from tests.unit.test_web_articles import tag_of, text_of
from tests.unit.test_web_entities import pages as entity_pages  # noqa: F401
from tests.unit.test_web_events import client, event, pages, report  # noqa: F401

AT = datetime(2026, 11, 4, 9, tzinfo=UTC)
EFFECT = BelegView("i10.0", "Wirkung", "Shell (Unternehmen): negativ, Monate", "margins squeezed", 10, "Report", "https://example.org/a", "Reuters")
FORECAST = BelegView("s7", "Quellenprognose", "Rates stay high.", None, 10, "Report", None, None)


def hypothesis(hypothesis_id, target="Shell", status="proposed", **fields):
    values = dict(
        id=hypothesis_id, event_id=7, target_id=hypothesis_id + 100, target_name=target, target_kind="company",
        target_merged_into_id=None, path="direct", via_name=None, relationship=None, number=1, status=status,
        direction="negative", horizon="months", mechanism="Freight costs squeeze margins.", counterarguments="Rates may ease.",
        classification="plausible", proposed_classification="evidenced", origin="llm", changed_at=AT, note=None,
        support=[EFFECT], against=[FORECAST],
    )
    return HypothesisView(**values | fields)


CHOICES = FormChoices(
    [TargetChoice(101, "Shell", "Unternehmen", True), TargetChoice(103, "ExxonMobil", "Unternehmen", False)],
    [WayChoice("direct", "direkt"), WayChoice("5", "indirekt: Shell konkurriert mit ExxonMobil")],
    [EFFECT, FORECAST],
)


@pytest.fixture
def impacts(monkeypatch, pages):
    pages["detail"] = EventDetail(event(7), [report(1, "new", founding=True)])
    calls = {}

    def review(name):
        def run(_engine, event_id, ids, note=""):
            calls[name] = (event_id, ids, note)
            if calls.get(f"{name}_error"):
                raise InvalidHypothesis(calls[f"{name}_error"])
            return calls.get(f"{name}_result", BulkResult(len(ids), []))
        return run

    monkeypatch.setattr(impact_service, "of_event", lambda _engine, event_id: calls.get("event", EventImpacts([], None)))
    monkeypatch.setattr(impact_service, "confirm", review("confirm"))
    monkeypatch.setattr(impact_service, "reject", review("reject"))
    monkeypatch.setattr(impact_service, "detail", lambda _engine, hypothesis_id: calls.get("detail"))
    monkeypatch.setattr(impact_service, "form_choices", lambda _engine, event_id: CHOICES)

    def change(_engine, hypothesis_id, form, support, against):
        calls["change"] = (hypothesis_id, form, support, against)
        if calls.get("change_error"):
            raise InvalidHypothesis(calls["change_error"])
        return True

    def create(_engine, event_id, form, support, against):
        calls["create"] = (event_id, form, support, against)
        return 42

    monkeypatch.setattr(impact_service, "change", change)
    monkeypatch.setattr(impact_service, "create", create)
    return calls


def test_the_event_page_lists_the_open_hypotheses_to_review_and_the_closed_apart(client, impacts):
    impacts["event"] = EventImpacts(
        [hypothesis(1), hypothesis(2, "TotalEnergies", "rejected", note="Not exposed.")],
        AssessmentView("qwen3.5:9b", 1, AT, 2, 1),
    )

    html = client.get("/events/7").text

    assert tag_of(html, "select-impact-1") is not None and tag_of(html, "select-impact-2") is None
    assert "disabled" in tag_of(html, "confirm-impacts") and "disabled" in tag_of(html, "reject-impacts")
    assert text_of(html, "impact-class-1") == "plausibel"
    assert "vom LLM: belegt" in re.search(r'data-testid="impact-class-1">(.*?)</td>', html, re.DOTALL).group(1)
    assert "1 dafür, 1 dagegen" in html
    assert tag_of(html, "closed-impacts") is not None and 'data-testid="impact-2"' in html
    assessment = re.search(r'data-testid="impacts-assessment">(.*?)</p>', html, re.DOTALL).group(1)
    assert "von qwen3.5:9b (Prompt 1): 2 Annahmen, 1 ausgelassen, weil Ziel, Weg oder Angaben nicht stimmten" in assessment
    assert tag_of(html, "new-impact") == '<a class="button button-secondary" href="/events/7/impacts/new" data-testid="new-impact">'
    assert tag_of(html, "section-nav-impacts") is not None


def test_without_hypotheses_the_section_says_so(client, impacts):
    html = client.get("/events/7").text
    assert text_of(html, "no-impacts") == "Keine offenen Wirkungsannahmen."
    assert text_of(html, "impacts-assessment").startswith("Noch nicht bewertet.")


def test_confirm_and_reject_pass_the_selection_and_reason_and_say_what_happened(client, impacts):
    response = client.post("/events/7/impacts/confirm", data={"hypothesis_id": ["1", "2"]})
    assert response.headers["location"] == "/events/7#impacts"
    assert impacts["confirm"] == (7, [1, 2], "")
    assert text_of(client.get("/events/7").text, "flash") == "2 Wirkungsannahmen bestätigt."

    impacts["reject_result"] = BulkResult(0, ["Shell (negativ)"])
    client.post("/events/7/impacts/reject", data={"hypothesis_id": ["1"], "note": "Wrong."})
    assert impacts["reject"] == (7, [1], "Wrong.")
    assert text_of(client.get("/events/7").text, "flash") == "Nichts verworfen. Bereits verworfen: Shell (negativ)."

    impacts["reject_error"] = "Bitte eine Begründung angeben."
    client.post("/events/7/impacts/reject", data={"hypothesis_id": ["1"]})
    assert text_of(client.get("/events/7").text, "flash") == "Nichts verworfen. Bitte eine Begründung angeben."


def detail():
    version = VersionView(1, "proposed", "negative", "months", "Freight costs.", "Rates may ease.", "plausible", "evidenced", "llm",
                          "qwen3.5:9b", 1, None, AT, [EFFECT], [])
    return HypothesisDetail(hypothesis(1, relationship=None), [version], CHOICES)


def test_a_hypothesis_page_shows_it_its_belege_and_versions_with_the_form_filled(client, impacts):
    impacts["detail"] = detail()

    html = client.get("/events/7/impacts/1").text

    assert text_of(html, "hypothesis-way") == "direkt"
    assert text_of(html, "hypothesis-class") == "plausibel"
    assert "vom LLM als belegt vorgeschlagen, nach der Regel herabgestuft" in html
    assert "checked" in tag_of(html, "support-i10.0") and "checked" in tag_of(html, "against-s7")
    assert "checked" not in tag_of(html, "support-s7")
    assert '<option value="negative" selected>' in html
    assert tag_of(html, "field-target") is None  # target and way stay
    assert "LLM (qwen3.5:9b, Prompt 1)" in re.search(r'data-testid="version-1">(.*?)</tr>', html, re.DOTALL).group(1)


def test_a_change_with_an_error_shows_the_form_again_with_the_entries(client, impacts):
    impacts["detail"] = detail()
    impacts["change_error"] = "Bitte Gegenargumente nennen."

    response = client.post("/events/7/impacts/1", data={"direction": "mixed", "mechanism": "New.", "support": ["s7"]})

    assert response.status_code == 200
    assert text_of(response.text, "form-error") == "Bitte Gegenargumente nennen."
    assert '<option value="mixed" selected>' in response.text
    assert "checked" in tag_of(response.text, "support-s7") and "checked" not in tag_of(response.text, "support-i10.0")
    assert impacts["change"][2] == ["s7"]


def test_a_change_saved_goes_back_to_the_hypothesis(client, impacts):
    impacts["detail"] = detail()
    response = client.post("/events/7/impacts/1", data={"direction": "mixed"})
    assert response.headers["location"] == "/events/7/impacts/1"


def test_a_new_hypothesis_chooses_target_and_way(client, impacts):
    html = client.get("/events/7/impacts/new").text
    assert "ExxonMobil (Unternehmen, nur über eine Beziehung)" in html
    assert '<option value="5">indirekt: Shell konkurriert mit ExxonMobil</option>' in html

    response = client.post("/events/7/impacts", data={"target": "103", "way": "5", "support": ["i10.0"], "against": ["s7"]})
    assert response.headers["location"] == "/events/7/impacts/42"
    event_id, form, support, against = impacts["create"]
    assert (event_id, form["target"], form["way"], support, against) == (7, "103", "5", ["i10.0"], ["s7"])


def test_the_entity_page_lists_the_hypotheses_about_it(client, entity_pages):
    from app.services.entity_service import EntityDetail
    from tests.unit.test_web_entities import entity

    entity_pages["detail"] = EntityDetail(entity(7), [], [], 0)
    entity_pages["impacts"] = [hypothesis(1, event_title="Freight rates squeeze margins")]

    html = client.get("/entities/7").text

    assert '<a href="/events/7/impacts/1">Freight rates squeeze margins</a>' in html
    assert text_of(html, "impact-class-1") == "plausibel"


def test_the_probability_stands_apart_with_what_lowered_it(client, impacts):
    from app.services.impact_service import Likelihood

    lowered = Likelihood("medium", "high", None, "yes", [FORECAST], "SHEL", -0.0612, -3.14, True)
    abstained = Likelihood(None, "high", impact_service.TOO_LITTLE_DATA)
    impacts["event"] = EventImpacts([hypothesis(1, likelihood=lowered), hypothesis(2, "TotalEnergies", likelihood=abstained), hypothesis(3, "Eni")], None)

    html = client.get("/events/7").text

    cell = re.search(r'data-testid="impact-probability-1">(.*?)</td>', html, re.DOTALL).group(1)
    assert all(part in cell for part in ("mittel", "vom LLM: hoch", "erwartet (1 Beleg)", "Vorlauf SHEL: -6,12 % abnormal, t -3,14: möglicherweise eingepreist"))
    total = re.search(r'data-testid="impact-probability-2">(.*?)</td>', html, re.DOTALL).group(1)
    assert "Enthaltung" in total and "Zu wenig Daten" in total
    assert "nicht bewertet" in re.search(r'data-testid="impact-probability-3">(.*?)</td>', html, re.DOTALL).group(1)


def test_the_form_offers_probability_or_abstention_filled_from_the_latest_version(client, impacts):
    from dataclasses import replace

    from app.services.impact_service import Likelihood

    impacts["detail"] = replace(detail(), hypothesis=hypothesis(1, likelihood=Likelihood(None, None, "Unclear exposure.", "yes")))

    html = client.get("/events/7/impacts/1").text

    assert '<option value="abstain" selected>Enthaltung</option>' in html
    assert 'value="Unclear exposure."' in tag_of(html, "field-abstention")
    assert '<option value="yes" selected>erwartet</option>' in html
