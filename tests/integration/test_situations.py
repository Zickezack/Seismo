"""Lagen in PostgreSQL (userstories.md, Ereignisidentität und zeitliche Entwicklung, story 4): proposed from groups of
events, confirmed by the analyst, then joined by new events - by their scores, or the LLM in doubt."""
import json
import threading
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import select, update

from app.db.models import Situation, SituationCheck, SituationMember
from app.jobs.handlers import build_handlers
from app.jobs.runner import run_next_job
from app.llm.limiter import LlmCallLimiter
from app.llm.ollama import OllamaClient
from app.repositories import event_repository
from app.services import event_service, job_service, platform_settings_service, situation_service, watchlist_service
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_event_filter import named
from tests.integration.test_events import reported
from tests.integration.test_extraction import Ollama

DAY = timedelta(days=1)
BACKGROUND = (
    "Floods cut rail lines in Valencia", "Dock workers strike in Rotterdam", "Earthquake strikes Xizang",
    "Wildfire spreads north of Athens", "Nvidia reports record quarterly revenue", "Chile elects a new president",
    "Measles outbreak grows in Texas", "Volcano erupts on Iceland's Reykjanes peninsula",
)
KINDS = (job_service.ASSIGN_SITUATION_JOB_KIND, job_service.PROPOSE_SITUATION_JOB_KIND)


def now():
    # The sweep compares with the database's clock: the events within the open period.
    return datetime.now(UTC)


def event(database, key, title, summary, actors=(), ago=DAY):
    """An event of one report, its own, `ago` before now."""
    report = {"title": title, "summary": summary, "place": None, "country": None, "actors": list(actors), "event_type": "armed_conflict"}
    [candidate] = reported(database, key, report, published_at=now() - ago)
    with database.begin() as connection:
        values = {"title": title, "event_type": "armed_conflict", "first_reported_at": now() - ago, "last_reported_at": now() - ago,
                  "founding_candidate_id": candidate}
        event_id = event_repository.create_event(connection, values)
        event_repository.assign(connection, candidate, event_id, "new", None, now() - ago)
    return event_id, candidate


@pytest.fixture
def red_sea(database):
    """Three Houthi attacks on shipping on three days, naming the Houthis, and a rate decision apart."""
    attacks = [
        event(database, "a", "Houthis fire missiles at a tanker in the Red Sea", "Houthi rebels attacked a tanker off Yemen.", ago=3 * DAY),
        event(database, "b", "Houthi drone strikes a cargo ship near Hodeidah", "The Houthis hit a cargo ship in the Red Sea off Yemen.",
              ago=2 * DAY),
        event(database, "c", "Shipping firms avoid the Red Sea after Houthi attacks",
              "Carriers reroute ships around Africa after Houthi rebels hit vessels off Yemen.", ago=DAY),
    ]
    houthis = named(database, attacks[0][1], "organization", "Houthis")
    for _, candidate in attacks[1:]:
        _mention(database, candidate, houthis)
    rates = event(database, "d", "ECB keeps key interest rates unchanged", "The ECB left its rates unchanged in Frankfurt.")
    # The rest of the news: a word shared by most events would weigh nothing.
    for number, title in enumerate(BACKGROUND):
        event(database, f"x{number}", title, title)
    return [event_id for event_id, _ in attacks], rates[0], houthis


def _mention(database, candidate_id, entity_id):
    from app.db.models import EntityAlias, EntityMention  # noqa: PLC0415

    with database.begin() as connection:
        alias = connection.execute(select(EntityAlias.id).where(EntityAlias.entity_id == entity_id)).scalar()
        connection.execute(EntityMention.__table__.insert().values(candidate_id=candidate_id, alias_id=alias, source="actor", position=0))


def run_jobs(database, ollama):
    llm = partial(OllamaClient, "http://ollama:11434", "qwen3.5:9b", 8192, transport=httpx2.MockTransport(ollama))
    stop = threading.Event()
    handlers = build_handlers(database, Path("/nonexistent"), LlmCallLimiter(database, stop), stop, "ops@example.org", llm)
    handlers = {kind: handlers[kind] for kind in KINDS}
    while run_next_job(database, handlers, "test", 10, on_renewed=lambda: None):
        pass


class AnswerBy:
    """Answers each "Lage zuordnen" by what `choose` makes of the event the question names; records into `ollama`."""

    def __init__(self, choose, ollama):
        self.choose, self.ollama = choose, ollama

    def __call__(self, request):
        body = json.loads(request.content)
        self.ollama.requests.append(body)
        content = json.dumps({"reason": "", "belongs_to": self.choose(body["messages"][1]["content"])})
        return httpx2.Response(200, json={"model": "m", "message": {"role": "assistant", "content": content}, "done_reason": "stop"})


def proposed(name="Houthi attacks on shipping in the Red Sea", events=(1, 2, 3), situation=True, known=None):
    return json.dumps({"reason": "Same campaign.", "known": known, "situation": situation, "name": name,
                       "description": "Houthi forces attack merchant ships off Yemen.", "events": list(events)})


def situations(database):
    with database.connect() as connection:
        return {row.id: row for row in connection.execute(select(Situation.__table__))}


def members(database, situation_id):
    with database.connect() as connection:
        rows = connection.execute(select(SituationMember.event_id, SituationMember.status, SituationMember.origin)
                                  .where(SituationMember.situation_id == situation_id)).all()
    return {row.event_id: (row.status, row.origin) for row in rows}


def confirmed_lage(database, red_sea):
    situation_service.sweep(database, llm_available=True)
    run_jobs(database, Ollama(proposed()))
    [situation_id] = situations(database)
    assert situation_service.confirm(database, [situation_id]).processed == 1
    return situation_id


def test_a_group_of_events_is_proposed_as_a_lage_and_counts_once_confirmed(database, red_sea):
    attacks, rates, _ = red_sea

    assert situation_service.sweep(database, llm_available=True).proposed == 1
    ollama = Ollama(proposed())
    run_jobs(database, ollama)

    [request] = ollama.requests
    user = request["messages"][1]["content"]
    assert "Houthis fire missiles at a tanker in the Red Sea" in user and "ECB" not in user
    assert user.endswith("Known situations:\nnone")
    [(situation_id, row)] = situations(database).items()
    assert (row.name, row.status) == ("Houthi attacks on shipping in the Red Sea", "proposed")
    assert members(database, situation_id) == {each: ("proposed", "proposal") for each in attacks}
    # Not in effect before the analyst confirms it.
    by_situation = event_service.parse_filter({"situation": str(situation_id)}).criteria
    assert event_service.overview(database, by_situation).events == []
    # Its events are taken: no second proposal of them.
    assert situation_service.sweep(database, llm_available=True).proposed == 0

    assert situation_service.confirm(database, [situation_id]).processed == 1

    assert {each.id for each in event_service.overview(database, by_situation).events} == set(attacks)
    assert set(members(database, situation_id).values()) == {("confirmed", "proposal")}


def test_a_group_the_llm_judges_no_lage_is_dismissed_and_not_asked_again(database, red_sea):
    situation_service.sweep(database, llm_available=True)
    run_jobs(database, Ollama(proposed(situation=False, events=())))

    [row] = situations(database).values()
    assert row.status == "dismissed"
    assert situation_service.sweep(database, llm_available=True).proposed == 0
    assert situation_service.overview(database).proposals == [] and situation_service.overview(database).dismissed == 1


def test_a_new_event_in_doubt_is_weighed_by_the_llm_and_joins_as_automatic(database, red_sea, monkeypatch):
    situation_id = confirmed_lage(database, red_sea)
    later, _ = event(database, "e", "Houthis claim attack on a bulk carrier in the Red Sea", "A bulk carrier was hit.", ago=DAY / 2)
    monkeypatch.setattr(situation_service, "JOIN_FROM", 0.99)
    monkeypatch.setattr(situation_service, "ASK_FROM", 0.01)

    assert situation_service.sweep(database, llm_available=True).asked >= 1
    ollama = Ollama(json.dumps({"reason": "Same campaign.", "belongs_to": [1]}))
    ollama_by_event = AnswerBy(lambda user: [1] if user.startswith("Event:\nHouthis claim") else [], ollama)
    run_jobs(database, ollama_by_event)

    [request] = [each for each in ollama.requests if each["messages"][1]["content"].startswith("Event:\nHouthis claim")]
    assert "Known situations:\n1. Houthi attacks on shipping in the Red Sea" in request["messages"][1]["content"]
    assert members(database, situation_id)[later] == ("automatic", "llm")
    assert len(members(database, situation_id)) == 4
    # Weighed: not asked again until it has a new report or a Lage is confirmed.
    assert situation_service.sweep(database, llm_available=True).asked == 0


def test_a_high_score_joins_alone_and_without_ollama_nothing_is_asked(database, red_sea, monkeypatch):
    situation_id = confirmed_lage(database, red_sea)
    later, _ = event(database, "e", "Houthis fire missiles at a tanker in the Red Sea again", "Houthi rebels attacked a tanker off Yemen.")
    monkeypatch.setattr(situation_service, "JOIN_FROM", 0.05)

    result = situation_service.sweep(database, llm_available=False)

    assert (result.joined, result.asked, result.proposed) == (1, 0, 0)
    assert members(database, situation_id)[later] == ("automatic", "score")


def test_a_dormant_lage_takes_a_new_event_only_as_a_proposed_resumption(database, red_sea, monkeypatch):
    situation_id = confirmed_lage(database, red_sea)
    attacks = red_sea[0]
    with database.begin() as connection:
        connection.execute(update(event_repository.Event).where(event_repository.Event.id.in_(attacks))
                           .values(first_reported_at=now() - 60 * DAY, last_reported_at=now() - 60 * DAY))
    later, _ = event(database, "e", "Houthis fire missiles at a tanker in the Red Sea again", "Houthi rebels attacked a tanker off Yemen.")
    monkeypatch.setattr(situation_service, "JOIN_FROM", 0.05)

    situation_service.sweep(database, llm_available=False)

    assert members(database, situation_id)[later] == ("proposed", "score")
    assert situation_service.detail(database, situation_id).situation.dormant
    assert [each.event_id for each in situation_service.detail(database, situation_id).proposed] == [later]


def test_an_event_removed_by_the_analyst_is_never_assigned_again(database, red_sea, monkeypatch):
    situation_id = confirmed_lage(database, red_sea)
    removed = red_sea[0][0]
    monkeypatch.setattr(situation_service, "JOIN_FROM", 0.0)

    assert situation_service.remove_events(database, situation_id, [removed]).processed == 1
    with database.begin() as connection:
        connection.execute(SituationCheck.__table__.delete())
    situation_service.sweep(database, llm_available=False)

    assert members(database, situation_id)[removed] == ("removed", "proposal")
    # The analyst may add it again.
    assert situation_service.add_event(database, situation_id, removed) == "Houthi attacks on shipping in the Red Sea"
    assert members(database, situation_id)[removed] == ("confirmed", "analyst")


def test_split_and_merge_move_the_events_and_keep_the_old_belongings(database, red_sea):
    situation_id = confirmed_lage(database, red_sea)
    attacks = red_sea[0]

    result = situation_service.split(database, situation_id, attacks[2:], "Red Sea shipping detours")
    assert result.processed == 1
    assert members(database, result.situation_id) == {attacks[2]: ("confirmed", "analyst")}
    assert members(database, situation_id)[attacks[2]] == ("removed", "proposal")

    assert situation_service.merge(database, situation_id, [result.situation_id]).processed == 1
    rows = situations(database)
    assert (rows[result.situation_id].status, rows[result.situation_id].merged_into_id) == ("merged", situation_id)
    # Its decision on the event stays: removed here before, it stays removed.
    assert members(database, situation_id)[attacks[2]] == ("removed", "proposal")
    assert members(database, result.situation_id)[attacks[2]][0] == "removed"


def test_the_analyst_s_decisions_are_all_or_nothing(database, red_sea):
    situation_id = confirmed_lage(database, red_sea)
    rates = red_sea[1]

    blocked = situation_service.confirm_events(database, situation_id, [red_sea[0][0], rates])
    assert (blocked.processed, blocked.blocked) == (0, [f"Ereignis {rates} (gehört nicht zur Lage)"])
    assert situation_service.reject(database, [situation_id]).blocked == ["Houthi attacks on shipping in the Red Sea (nicht vorgeschlagen)"]
    with pytest.raises(situation_service.InvalidSituation, match="Name: bitte angeben."):
        situation_service.split(database, situation_id, [red_sea[0][0]], " ")
    assert situation_service.merge(database, situation_id, [999]).blocked == ["Lage #999 (nicht gefunden)"]


def test_a_rejected_proposal_keeps_its_events_from_being_proposed_together_again(database, red_sea):
    situation_service.sweep(database, llm_available=True)
    run_jobs(database, Ollama(proposed()))
    [situation_id] = situations(database)

    assert situation_service.reject(database, [situation_id]).processed == 1

    assert situation_service.sweep(database, llm_available=True).proposed == 0


def test_the_budget_holds_the_forming_of_lagen(database, red_sea):
    platform_settings_service.save(database, {"situation_llm_calls_per_hour": "0"})

    assert situation_service.sweep(database, llm_available=True) == situation_service.SweepResult()


def test_a_watchlist_follows_a_lage(database, red_sea):
    situation_id = confirmed_lage(database, red_sea)
    watched = watchlist_service.create(database, "Seewege").id
    watchlist_service.add(database, watched, "situation", str(situation_id))

    criteria = event_service.parse_filter({"watchlist": str(watched)}).criteria

    assert {each.id for each in event_service.overview(database, criteria).events} == set(red_sea[0])
    [item] = watchlist_service.detail(database, watched).items
    assert (item.kind_label, item.label, item.href) == ("Lage", "Houthi attacks on shipping in the Red Sea", f"/situations/{situation_id}")


def test_the_lage_page_shows_its_events_in_time_order_with_their_sources(database, red_sea):
    situation_id = confirmed_lage(database, red_sea)

    detail = situation_service.detail(database, situation_id)

    assert [each.event_id for each in detail.members] == red_sea[0]
    assert detail.members[0].event.report_count == 1
    assert [source.events for source in detail.sources] == [1, 1, 1]
    lagen = situation_service.of_event(database, red_sea[0][0])
    assert [(each.situation.id, each.status_label) for each in lagen.situations] == [(situation_id, "bestätigt")]
    assert situation_service.of_event(database, red_sea[1]).choices[0].id == situation_id


def test_a_proposal_is_shown_the_lagen_proposed_before_it_ran(database, red_sea):
    """Proposals of one sweep run one after the other: a later one sees what an earlier one proposed."""
    from app.repositories import situation_repository  # noqa: PLC0415

    situation_service.sweep(database, llm_available=True)
    later, _ = event(database, "e", "Houthis seize a car carrier in the Red Sea off Yemen", "Houthi rebels seized a ship off Yemen.")
    with database.begin() as connection:
        earlier = situation_repository.insert_situation(connection, "Red Sea shipping crisis", "Ships attacked off Yemen.", "proposed")
        situation_repository.add_member(connection, earlier, later, "proposed", "proposal", None)
    ollama = Ollama(proposed(known=1, events=(1, 2, 3)))

    run_jobs(database, ollama)

    [request] = ollama.requests
    assert "Known situations:\n1. Red Sea shipping crisis: Ships attacked off Yemen." in request["messages"][1]["content"]
    assert set(members(database, earlier)) == {later, *red_sea[0]}
    assert {status for status, _ in members(database, earlier).values()} == {"proposed"}
