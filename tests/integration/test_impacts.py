"""Wirkungsannahmen in PostgreSQL (userstories.md, Markt- und Wirkungsmodell, story 1): which events are assessed and
when, what the LLM is shown, the rule's classification, the versions an assessment appends, and the analyst's review,
change and own hypothesis."""
import json
import threading
from datetime import timedelta
from functools import partial
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import func, insert, select, update

from app.db.models import Entity, EntityAlias, EntityMention, ImpactAssessment, ImpactHypothesisVersion, Job
from app.jobs.handlers import build_handlers
from app.jobs.runner import run_next_job
from app.llm.limiter import LlmCallLimiter
from app.llm.ollama import OllamaClient
from app.repositories import impact_repository
from app.services import impact_service, job_service, platform_settings_service, relationship_service
from app.services.impact_service import InvalidHypothesis
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_event_context import event_of, reported, statement
from tests.integration.test_event_filter import named
from tests.integration.test_extraction import Ollama

KIND = job_service.ASSESS_IMPACTS_JOB_KIND
QUOTE = "Shell's refining margins were squeezed by soaring freight rates"
REPORT = {
    "event_type": "supply_chain_disruption", "title": "Freight rates squeeze oil majors' margins",
    "summary": "Tanker freight rates hit oil majors' refining margins.", "country": None, "place": None,
    "actors": ["Shell", "TotalEnergies"],
    "impacts": [{"target": "Shell", "target_kind": "company", "direction": "negative", "horizon": "months",
                 "description": "Refining margins squeezed.", "quote": QUOTE}],
}


def hypothesis(target, path="direct", via=None, direction="negative", support=(1,), against=(), classification="evidenced", mechanism="Higher costs."):
    return {
        "target": target, "path": path, "via": via, "direction": direction, "horizon": "months", "mechanism": mechanism,
        "counterarguments": "Freight rates may ease.", "support": list(support), "against": list(against), "classification": classification,
    }


def answer(*hypotheses):
    return json.dumps({"reason": "", "hypotheses": list(hypotheses)})


@pytest.fixture
def oil(database):
    """An event whose report names Shell's margins squeezed - its effect linked to Shell -, with TotalEnergies among its
    actors and a source's forecast; ExxonMobil, which it does not name, competes with Shell."""
    candidate = reported(database, "oil", REPORT, [statement("source_forecast", "Freight rates stay high into next year.")])
    shell = named(database, candidate, "company", "Shell")
    total = named(database, candidate, "company", "TotalEnergies", position=1)
    with database.begin() as connection:
        alias = connection.execute(select(EntityAlias.id).where(EntityAlias.entity_id == shell)).scalar()
        connection.execute(insert(EntityMention).values(candidate_id=candidate, alias_id=alias, source="impact", position=0))
    with database.begin() as connection:
        exxon = connection.execute(insert(Entity).values(kind="company", name="ExxonMobil").returning(Entity.id)).scalar_one()
        connection.execute(insert(EntityAlias).values(entity_id=exxon, scheme="name", value="ExxonMobil", key="exxonmobil", match="founding"))
    relationship_service.add(database, shell, "competitor", str(exxon), note="Annual report 2025.")
    return {"event_id": event_of(database, candidate), "candidate": candidate, "shell": shell, "total": total, "exxon": exxon}


def run_jobs(database, ollama):
    llm = partial(OllamaClient, "http://ollama:11434", "qwen3.5:9b", 8192, transport=httpx2.MockTransport(ollama))
    stop = threading.Event()
    handlers = build_handlers(database, Path("/nonexistent"), LlmCallLimiter(database, stop), stop, "ops@example.org", llm)
    while run_next_job(database, {KIND: handlers[KIND]}, "test", 10, on_renewed=lambda: None):
        pass


def assess(database, event_id, content):
    """Queue the event's assessment and answer it with `content`; what the LLM was asked."""
    with database.begin() as connection:
        job_service.enqueue(connection, KIND, {"event_id": event_id}, dedupe_key=f"{KIND}:{event_id}", priority=1)
    ollama = Ollama(content)
    run_jobs(database, ollama)
    return ollama.requests


def by_target(database, event_id):
    return {view.target_name: view for view in impact_service.of_event(database, event_id).hypotheses}


def forget_asking(database):
    """As if the assessments and their jobs were more than a day ago."""
    with database.begin() as connection:
        connection.execute(update(Job).where(Job.kind == KIND).values(created_at=func.now() - timedelta(days=2)))
        connection.execute(update(ImpactAssessment).values(created_at=func.now() - timedelta(days=2)))


def test_the_llm_is_shown_the_belege_targets_and_relationships_and_chooses_from_them(database, oil):
    [request] = assess(database, oil["event_id"], answer())

    shown = request["messages"][1]["content"]
    assert f'1. [effect named] Shell (company): negative, months - Refining margins squeezed. - "{QUOTE}"' in shown
    assert "2. [source_forecast] Freight rates stay high into next year." in shown
    assert "1. Shell (company)\n2. TotalEnergies (company)\n3. ExxonMobil (company) - not named by the event" in shown
    assert "1. ExxonMobil competes with Shell" in shown or "1. Shell competes with ExxonMobil" in shown
    item = request["format"]["properties"]["hypotheses"]["items"]["properties"]
    assert item["target"] == {"enum": [1, 2, 3]} and item["via"] == {"enum": [1, None]}
    assert item["support"]["items"] == {"enum": [1, 2]}


def test_the_rule_classifies_and_what_does_not_hold_is_left_out(database, oil):
    assess(database, oil["event_id"], answer(
        hypothesis(1),  # Shell: the report names this effect - belegt
        hypothesis(2),  # TotalEnergies: the effect cited is Shell's - at most plausibel
        hypothesis(3, "indirect", 1, "positive", support=(), classification="evidenced"),  # ExxonMobil, as competitor
        hypothesis(3),  # not named: never direct
        hypothesis(1, direction="positive"),  # Shell directly once more
        hypothesis(2, "indirect", 1),  # the relationship is not TotalEnergies'
        hypothesis(2, "indirect", None),  # indirect without a relationship
    ))

    found = by_target(database, oil["event_id"])
    assert set(found) == {"Shell", "TotalEnergies", "ExxonMobil"}
    assert (found["Shell"].classification, found["Shell"].capped_from) == ("evidenced", None)
    assert (found["TotalEnergies"].classification, found["TotalEnergies"].capped_from) == ("plausible", "belegt")
    assert found["ExxonMobil"].classification == "plausible" and found["ExxonMobil"].way_label.startswith("indirekt über Shell")
    assert [each.text for each in found["Shell"].support] == ["Shell (Unternehmen): negativ, Monate - Refining margins squeezed."]
    assert all(each.status == "proposed" and each.number == 1 for each in found.values())
    assessment = impact_service.of_event(database, oil["event_id"]).assessment
    assert (assessment.hypotheses, assessment.dropped, assessment.prompt_version) == (3, 4, impact_service.PROMPT_VERSION)


def test_a_new_assessment_changes_only_what_is_still_proposed(database, oil):
    event_id = oil["event_id"]
    assess(database, event_id, answer(hypothesis(1), hypothesis(2), hypothesis(3, "indirect", 1, "positive", support=())))
    found = by_target(database, event_id)
    assert impact_service.confirm(database, event_id, [found["Shell"].id]).processed == 1
    assert impact_service.reject(database, event_id, [found["TotalEnergies"].id], "Not exposed to refining.").processed == 1

    # Shell and TotalEnergies read differently now, ExxonMobil is no longer proposed.
    assess(database, event_id, answer(hypothesis(1, direction="mixed"), hypothesis(2, direction="positive")))
    found = by_target(database, event_id)
    assert (found["Shell"].status, found["Shell"].direction, found["Shell"].number) == ("confirmed", "negative", 2)
    assert (found["TotalEnergies"].status, found["TotalEnergies"].note) == ("rejected", "Not exposed to refining.")
    assert (found["ExxonMobil"].status, found["ExxonMobil"].number) == ("superseded", 2)

    # Proposed again: back as a new version; the same answer again appends nothing.
    for _ in range(2):
        assess(database, event_id, answer(hypothesis(3, "indirect", 1, "positive", support=())))
    exxon = by_target(database, event_id)["ExxonMobil"]
    assert (exxon.status, exxon.number) == ("proposed", 3)
    versions = impact_service.detail(database, exxon.id).versions
    assert [each.status for each in versions] == ["proposed", "superseded", "proposed"]
    assert versions[0].origin_label == f"LLM (qwen3.5:9b, Prompt {impact_service.PROMPT_VERSION})"


def test_an_event_is_due_once_then_again_after_a_new_report_at_most_once_a_day(database, oil):
    event_id = oil["event_id"]
    with database.connect() as connection:
        assert event_id in impact_repository.due(connection, KIND, impact_service.TARGET_KINDS, impact_service.AGAIN_AFTER, 10)
    assert impact_service.enqueue_due(database)
    assert impact_service.enqueue_due(database) == []
    run_jobs(database, Ollama(answer(hypothesis(1))))

    forget_asking(database)
    assert impact_service.enqueue_due(database) == []  # no new report
    later = reported(database, "later", REPORT | {"title": "Shell's margins fall further"})
    with database.begin() as connection:
        connection.execute(update(ImpactAssessment).values(reports=0))  # as if asked before this report
    assert later
    assert impact_service.enqueue_due(database)


def test_nothing_is_queued_while_the_budget_is_zero(database, oil):
    platform_settings_service.save(database, {"impact_llm_calls_per_hour": "0"})
    assert impact_service.enqueue_due(database) == []


def test_an_event_naming_no_company_sector_or_commodity_is_not_assessed(database):
    candidate = reported(database, "rates", REPORT | {"title": "Rates unchanged", "actors": ["ECB"]})
    named(database, candidate, "authority", "ECB")
    event_id = event_of(database, candidate)
    with database.connect() as connection:
        assert event_id not in impact_repository.due(connection, KIND, impact_service.TARGET_KINDS, impact_service.AGAIN_AFTER, 10)


def form(**fields):
    return {"direction": "negative", "horizon": "weeks", "mechanism": "Costs rise.", "counterarguments": "Hedged.", **fields}


def test_the_analyst_adds_one_by_hand_capped_by_the_rule_and_never_twice(database, oil):
    event_id = oil["event_id"]
    support = [impact_service.ref_key({"candidate_id": oil["candidate"], "impact": 0})]

    shell_id = impact_service.create(database, event_id, form(target=str(oil["shell"]), classification="evidenced"), support, [])
    shell = impact_service.detail(database, shell_id).hypothesis
    assert (shell.status, shell.classification, shell.origin) == ("confirmed", "evidenced", "analyst")
    exxon_id = impact_service.create(
        database, event_id, form(target=str(oil["exxon"]), way=str(_relationship(database, oil)), classification="evidenced"), [], []
    )
    assert impact_service.detail(database, exxon_id).hypothesis.classification == "plausible"
    total_id = impact_service.create(database, event_id, form(target=str(oil["total"]), classification="evidenced"), [], [])
    assert impact_service.detail(database, total_id).hypothesis.classification == "speculative"

    with pytest.raises(InvalidHypothesis, match="gibt es schon"):
        impact_service.create(database, event_id, form(target=str(oil["shell"])), [], [])
    with pytest.raises(InvalidHypothesis, match="Direkt nur auf eine Entität, die seine Berichte nennen"):
        impact_service.create(database, event_id, form(target=str(oil["exxon"])), [], [])
    with pytest.raises(InvalidHypothesis, match="Mechanismus"):
        impact_service.create(database, event_id, form(target=str(oil["total"]), way=str(_relationship(database, oil)), mechanism=" "), [], [])


def test_a_change_is_a_new_confirmed_version_keeping_target_and_way(database, oil):
    event_id = oil["event_id"]
    assess(database, event_id, answer(hypothesis(1)))
    shell = by_target(database, event_id)["Shell"]

    assert impact_service.change(database, shell.id, form(direction="mixed", note="Hedging offsets part."), [], [])

    detail = impact_service.detail(database, shell.id)
    assert (detail.hypothesis.number, detail.hypothesis.status, detail.hypothesis.direction) == (2, "confirmed", "mixed")
    # Without its Beleg the rule allows no more than speculative.
    assert detail.hypothesis.classification == "speculative"
    assert [(each.number, each.origin) for each in detail.versions] == [(1, "llm"), (2, "analyst")]
    with database.connect() as connection:
        assert connection.execute(select(func.count()).select_from(ImpactHypothesisVersion)).scalar_one() == 2


def test_reviews_are_all_or_nothing_and_a_rejection_needs_a_reason(database, oil):
    event_id = oil["event_id"]
    assess(database, event_id, answer(hypothesis(1), hypothesis(2)))
    found = by_target(database, event_id)
    ids = [found["Shell"].id, found["TotalEnergies"].id]
    impact_service.confirm(database, event_id, ids[:1])

    result = impact_service.confirm(database, event_id, ids)
    assert (result.processed, result.blocked) == (0, ["Shell (negativ)"])
    assert by_target(database, event_id)["TotalEnergies"].status == "proposed"
    with pytest.raises(InvalidHypothesis, match="Begründung"):
        impact_service.reject(database, event_id, ids, " ")
    # Another event's hypothesis is not this one's to review.
    assert impact_service.confirm(database, event_id + 1000, ids).processed == 0


def test_the_entity_page_lists_the_hypotheses_about_it(database, oil):
    assess(database, oil["event_id"], answer(hypothesis(1), hypothesis(2)))
    impact_service.reject(database, oil["event_id"], [by_target(database, oil["event_id"])["TotalEnergies"].id], "No.")

    [about_shell] = impact_service.about_entity(database, oil["shell"])
    assert (about_shell.event_title, about_shell.classification) == (REPORT["title"], "evidenced")
    assert impact_service.about_entity(database, oil["total"]) == []


def _relationship(database, oil):
    with database.connect() as connection:
        [relation] = relationship_service.in_effect_around(connection, [oil["shell"]])
    return relation.id


def test_a_disputed_relationship_opens_no_way(database, oil):
    relationship_service.review(database, oil["shell"], [_relationship(database, oil)], "disputed", "Different markets.")

    with database.connect() as connection:
        shown = impact_service.context(connection, oil["event_id"])
    assert [each.name for each in shown.targets] == ["Shell", "TotalEnergies"] and shown.relationships == []
