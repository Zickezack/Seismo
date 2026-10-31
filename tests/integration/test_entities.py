"""Entity recognition in PostgreSQL: names resolved to entities by identifier, name form or the LLM's judgement of a
similar name - uncertain then - the analyst's corrections, and the starter list of sectors and commodities - against a
simulated Ollama."""
import json
import logging
import threading
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import Entity, EntityAlias, EntityMention, EntityRecognition, EventCandidate, JobStatus
from app.jobs.errors import JobDeferred
from app.jobs.handlers import build_handlers
from app.jobs.runner import run_next_job
from app.llm.limiter import LlmCallLimiter
from app.repositories import extraction_repository
from app.services import entity_correction_service, entity_service, job_service, seed_service
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_events import assign_all, llm
from tests.integration.test_extraction import Ollama, source
from tests.integration.test_ingestion import jobs
from tests.integration.test_normalization import article

KIND = job_service.RECOGNISE_ENTITIES_JOB_KIND


def extracted(database, key, actors=(), impacts=()):
    """An article whose extraction found one report with these actors (name, role, quote) and impact targets
    (target, quote); the extraction's and the report's id."""
    article_id = article(database, source(database, f"Q{key}"), key, url=f"https://news.example/{key}.html")
    report = {
        "event_type": "armed_conflict", "status": "update", "title": f"Report {key}", "summary": "Summary.", "country": None,
        "place": None, "actors": [{"name": name, "role": role, "quote": quote} for name, role, quote in actors],
        "impacts": [{"target": target, "target_kind": "country", "direction": "negative", "horizon": "weeks", "description": "",
                     "quote": quote} for target, quote in impacts],
        "statements": [],
    }
    values = {"article_id": article_id, "model": "m", "schema_version": "2", "duration_ms": 1, "input_chars": 1, "truncated": False}
    with database.begin() as connection:
        extraction_id = extraction_repository.insert_extraction(connection, values, [report])
        candidate_id = connection.execute(select(EventCandidate.id).where(EventCandidate.extraction_id == extraction_id)).scalar_one()
    return extraction_id, candidate_id


def item(mention, kind, name=None, **fields):
    """An answer's item; a person with an office, as a person needs one."""
    values = {"mention": mention, "public_role": "President" if kind == "person" else None, "kind": kind, "name": name,
              "short_name": None, "same_as": None, "parent": None, "country_code": None, "category": None, "identifiers": []}
    return values | fields


def recognition(*items, relationships=()):
    return json.dumps({"entities": list(items), "relationships": list(relationships)})


def recognise_all(database, ollama):
    entity_service.enqueue_due(database)
    stop = threading.Event()
    handlers = build_handlers(database, Path("/nonexistent"), LlmCallLimiter(database, stop), stop, "ops@example.org", llm(ollama))
    handlers = {KIND: handlers[KIND]}
    while run_next_job(database, handlers, "test", 10, on_renewed=lambda: None):
        pass


def rows(database, table, *where):
    with database.connect() as connection:
        return connection.execute(select(table.__table__).where(*where).order_by(table.__table__.primary_key.columns.values()[0])).all()


def entity_named(database, name):
    [row] = rows(database, Entity, Entity.name == name)
    return row


def aliases_of(database, entity_id):
    return [(alias.scheme, alias.value, alias.match, alias.uncertain) for alias in rows(database, EntityAlias, EntityAlias.entity_id == entity_id)]


def question(ollama, number=-1):
    return ollama.requests[number]["messages"][1]["content"]


def test_names_become_entities_with_their_name_forms_and_every_place_they_are_named(database, caplog):
    extraction_id, candidate_id = extracted(
        database, "a", actors=[("Vladimir Zelensky", "President of Ukraine", "said Vladimir Zelensky"), ("Residents", "", "residents")],
        impacts=[("Ukraine", "Ukraine's exports"), ("vladimir zelensky", "Zelensky's office")],
    )
    ollama = Ollama(recognition(
        item(1, "person", "Volodymyr Zelenskyy"), item(2, "none"), item(3, "country", "Ukraine", country_code="UA"),
    ))

    with caplog.at_level(logging.INFO, logger=entity_service.__name__):
        recognise_all(database, ollama)

    person, country = entity_named(database, "Volodymyr Zelenskyy"), entity_named(database, "Ukraine")
    assert (person.kind, country.kind) == ("person", "country")
    assert aliases_of(database, person.id) == [("name", "Vladimir Zelensky", "founding", False), ("name", "Volodymyr Zelenskyy", "founding", False)]
    assert aliases_of(database, country.id) == [("name", "Ukraine", "founding", False), ("iso3166", "UA", "founding", False)]
    mentions = [(mention.candidate_id, mention.source, mention.position) for mention in rows(database, EntityMention)]
    assert mentions == [(candidate_id, "actor", 0), (candidate_id, "impact", 1), (candidate_id, "impact", 0)]
    [run] = rows(database, EntityRecognition)
    assert (run.extraction_id, run.model, run.names, run.linked) == (extraction_id, "qwen3.5:9b", 3, 2)
    assert "Known entities:\nnone" in question(ollama)
    assert [job.status for job in jobs(database, KIND)] == [JobStatus.SUCCEEDED]
    # Ids and counts only (datamodel.md, rule 13).
    [record] = [record for record in caplog.records if record.getMessage() == "Entities recognised"]
    assert (record.extraction_id, record.names, record.linked, record.founded, record.uncertain) == (extraction_id, 3, 2, 2, 0)
    assert "Zelensk" not in caplog.text


def test_a_known_name_form_or_identifier_resolves_to_its_entity_for_certain(database):
    extracted(database, "a", actors=[("Volodymyr Zelenskyy", "president", "q"), ("Ukraine", "state", "q")])
    recognise_all(database, Ollama(recognition(item(1, "person", "Volodymyr Zelenskyy"), item(2, "country", "Ukraine", country_code="UA"))))
    extracted(database, "b", actors=[("President Zelenskyy", "president", "q"), ("Ukraina", "state", "q")])

    recognise_all(database, Ollama(recognition(item(1, "person", "Volodymyr Zelenskyy"), item(2, "country", "Ukraine", country_code="UA"))))

    person, country = entity_named(database, "Volodymyr Zelenskyy"), entity_named(database, "Ukraine")
    assert ("name", "President Zelenskyy", "name", False) in aliases_of(database, person.id)
    assert ("name", "Ukraina", "identifier", False) in aliases_of(database, country.id)
    assert len(rows(database, Entity)) == 2


def test_a_similar_name_the_llm_judges_the_same_is_linked_and_flagged_uncertain(database):
    extracted(database, "a", actors=[("Volodymyr Zelenskyy", "president", "q")])
    recognise_all(database, Ollama(recognition(item(1, "person", "Volodymyr Zelenskyy"))))
    person = entity_named(database, "Volodymyr Zelenskyy")
    _, candidate_id = extracted(database, "b", actors=[("Volodymyr Zelensky", "president", "q")])
    ollama = Ollama(recognition(item(1, "person", "Volodymyr Zelensky", same_as=1)))

    recognise_all(database, ollama)

    assert "Known entities:\n1. Volodymyr Zelenskyy (person)" in question(ollama)
    [alias] = rows(database, EntityAlias, EntityAlias.value == "Volodymyr Zelensky")
    assert (alias.entity_id, alias.match, alias.uncertain) == (person.id, "llm", True)
    assert 0.8 < alias.similarity < 0.9
    [mention] = rows(database, EntityMention, EntityMention.candidate_id == candidate_id)
    assert mention.alias_id == alias.id

    # The same name again finds the uncertain form: still uncertain, and so is a form recognised with it.
    extracted(database, "c", actors=[("Volodymyr Zelensky", "president", "q")])
    recognise_all(database, Ollama(recognition(item(1, "person", "Volodymyr Zelensky", short_name="Zelensky"))))
    assert ("name", "Zelensky", "name", True) in aliases_of(database, person.id)
    assert len(rows(database, Entity)) == 1


def test_a_similar_name_the_llm_does_not_judge_the_same_founds_an_entity_of_its_own(database):
    extracted(database, "a", actors=[("Central Bank of Brazil", "rate setter", "q")])
    recognise_all(database, Ollama(recognition(item(1, "authority", "Central Bank of Brazil"))))
    extracted(database, "b", actors=[("Central Bank of Chile", "rate setter", "q")])
    ollama = Ollama(recognition(item(1, "authority", "Central Bank of Chile", same_as=None)))

    recognise_all(database, ollama)

    assert "1. Central Bank of Brazil (authority)" in question(ollama)
    chile = entity_named(database, "Central Bank of Chile")
    assert aliases_of(database, chile.id) == [("name", "Central Bank of Chile", "founding", False)]


def test_an_entity_with_a_similar_name_that_came_since_the_llm_was_asked_defers_the_decision(database):
    first, _ = extracted(database, "a", actors=[("Volodymyr Zelensky", "president", "q")])
    second, _ = extracted(database, "b", actors=[("Volodymyr Zelenskyy", "president", "q")])
    stop = threading.Event()
    with llm(Ollama(recognition(item(1, "person", "Volodymyr Zelensky"))))() as client:
        late = entity_service.recognise(database, first, client, LlmCallLimiter(database, stop))
    with llm(Ollama(recognition(item(1, "person", "Volodymyr Zelenskyy"))))() as client:
        early = entity_service.recognise(database, second, client, LlmCallLimiter(database, stop))
    with database.begin() as connection:
        early(connection)

    with pytest.raises(JobDeferred), database.begin() as connection:
        late(connection)

    assert len(rows(database, Entity)) == 1 and len(rows(database, EntityRecognition)) == 1


def test_a_recognition_is_stored_once(database):
    extraction_id, _ = extracted(database, "a", actors=[("Siemens", "maker", "q")])
    stop = threading.Event()
    effects = []
    for _ in range(2):
        with llm(Ollama(recognition(item(1, "company", "Siemens"))))() as client:
            effects.append(entity_service.recognise(database, extraction_id, client, LlmCallLimiter(database, stop)))
    for effect in effects:
        with database.begin() as connection:
            effect(connection)

    assert len(rows(database, Entity)) == 1 and len(rows(database, EntityMention)) == 1


def test_reports_without_names_are_recognised_without_asking(database):
    extracted(database, "a")

    recognise_all(database, Ollama(status=503))

    [run] = rows(database, EntityRecognition)
    assert (run.model, run.names, run.linked) == (None, 0, 0)


def test_an_unusable_answer_is_retried_with_the_job(database):
    extracted(database, "a", actors=[("Siemens", "maker", "q")])

    recognise_all(database, Ollama('{"entities": [{"mention": 1}]}'))

    [job] = jobs(database, KIND)
    assert (job.status, job.attempts) == (JobStatus.QUEUED, 1) and "ausserhalb des Schemas" in job.last_error
    assert rows(database, EntityRecognition) == []


def test_sectors_and_commodities_come_only_from_the_imported_starter_list(database):
    extracted(database, "a", impacts=[("Banking sector", "banks' shares")])
    ollama = Ollama(recognition(item(1, "sector", "Banking", category=None)))
    recognise_all(database, ollama)
    assert ollama.requests[0]["format"]["properties"]["entities"]["items"]["properties"]["category"] == {"enum": [None]}
    assert rows(database, Entity) == []

    seed_service.import_entities(database)
    extracted(database, "b", impacts=[("Banking sector", "banks' shares")])
    ollama = Ollama(recognition(item(1, "sector", "Banking", category="banking")))
    recognise_all(database, ollama)

    assert "banking: banking" in question(ollama) and "crude-oil: crude oil (Brent, WTI)" in question(ollama)
    banks = entity_named(database, "Banken")
    assert ("name", "Banking sector", "identifier", False) in aliases_of(database, banks.id)


def test_the_starter_list_is_imported_once(database):
    first = seed_service.import_entities(database)
    second = seed_service.import_entities(database)

    total = len(seed_service.entity_catalogue())
    assert (first.imported, second.imported, second.skipped) == (total, 0, total)
    assert seed_service.entities_status(database).imported == total
    oil = entity_named(database, "Rohöl")
    assert aliases_of(database, oil.id) == [("catalogue", "crude-oil", "seed", False), ("name", "Rohöl", "seed", False), ("name", "crude oil", "seed", False)]


def test_a_subsidiary_gets_its_parent_known_or_founded(database):
    extracted(database, "a", actors=[("Google", "search engine", "q"), ("YouTube", "video site", "q")])

    recognise_all(database, Ollama(recognition(
        item(1, "subsidiary", "Google", parent="Alphabet"), item(2, "subsidiary", "YouTube", parent="Alphabet Inc."),
    )))

    alphabet = entity_named(database, "Alphabet")
    assert alphabet.kind == "company"
    assert (entity_named(database, "Google").parent_id, entity_named(database, "YouTube").parent_id) == (alphabet.id, alphabet.id)
    assert len(rows(database, Entity, Entity.kind == "company")) == 1


def two_zelenskys(database):
    """One entity with a certain and an uncertain name form, each named by one report."""
    extracted(database, "a", actors=[("Volodymyr Zelenskyy", "president", "q")])
    recognise_all(database, Ollama(recognition(item(1, "person", "Volodymyr Zelenskyy"))))
    extracted(database, "b", actors=[("Volodymyr Zelensky", "president", "q")])
    recognise_all(database, Ollama(recognition(item(1, "person", "Volodymyr Zelensky", same_as=1))))
    person = entity_named(database, "Volodymyr Zelenskyy")
    certain, uncertain = rows(database, EntityAlias, EntityAlias.entity_id == person.id)
    return person, certain, uncertain


def test_confirming_makes_an_uncertain_name_form_certain(database):
    person, certain, uncertain = two_zelenskys(database)

    result = entity_correction_service.confirm(database, person.id, [certain.id, uncertain.id])

    assert (result.processed, result.blocked) == (1, [])
    [alias] = rows(database, EntityAlias, EntityAlias.id == uncertain.id)
    assert alias.uncertain is False and alias.confirmed_at is not None
    assert entity_service.detail(database, person.id).entity.uncertain_count == 0


def test_a_name_form_of_another_entity_blocks_a_confirmation(database):
    person, _, uncertain = two_zelenskys(database)

    result = entity_correction_service.confirm(database, person.id + 1, [uncertain.id])

    assert result.processed == 0 and result.blocked == ["Volodymyr Zelensky (nicht bei dieser Entität)"]


def test_splitting_off_a_name_form_takes_its_reports_into_an_entity_of_its_own(database):
    person, certain, uncertain = two_zelenskys(database)

    result = entity_correction_service.split(database, person.id, [uncertain.id])

    assert result.processed == 1
    new = entity_named(database, "Volodymyr Zelensky")
    assert (new.id, new.kind) == (result.entity_id, "person")
    assert aliases_of(database, new.id) == [("name", "Volodymyr Zelensky", "split", False)]
    assert entity_service.detail(database, new.id).mention_total == 1
    assert entity_service.detail(database, person.id).mention_total == 1


def test_a_split_keeps_a_name_form_takes_one_for_the_new_name_and_never_takes_a_starter_key(database):
    person, certain, uncertain = two_zelenskys(database)
    extracted(database, "c", actors=[("Ukraine", "state", "q")])
    recognise_all(database, Ollama(recognition(item(1, "country", "Ukraine", country_code="UA"))))
    country_name, country_code = rows(database, EntityAlias, EntityAlias.entity_id == entity_named(database, "Ukraine").id)
    seed_service.import_entities(database)
    oil = entity_named(database, "Rohöl")
    oil_aliases = rows(database, EntityAlias, EntityAlias.entity_id == oil.id)

    every = entity_correction_service.split(database, person.id, [certain.id, uncertain.id])
    code_only = entity_correction_service.split(database, country_code.entity_id, [country_code.id])
    key = entity_correction_service.split(database, oil.id, [alias.id for alias in oil_aliases[:2]])

    assert every.blocked == ["Volodymyr Zelenskyy (behielte keine Namensform)"]
    assert code_only.blocked == ["keine Namensform ausgewählt: die neue Entität braucht einen Namen"]
    assert key.blocked == ["crude-oil (Starterliste)"]
    assert len(rows(database, Entity)) == 2 + len(seed_service.entity_catalogue())


def test_merging_moves_name_forms_reports_and_subsidiaries_and_keeps_the_merged_one(database):
    extracted(database, "a", actors=[("Alphabet", "owner", "q"), ("Alphabet Holdings", "owner", "q"), ("Waymo", "robotaxi", "q")])
    recognise_all(database, Ollama(recognition(
        item(1, "company", "Alphabet"), item(2, "company", "Alphabet Holdings"), item(3, "subsidiary", "Waymo", parent="Alphabet Holdings"),
    )))
    alphabet, holdings = entity_named(database, "Alphabet"), entity_named(database, "Alphabet Holdings")

    similar = entity_correction_service.similar_entities(database, alphabet.id)
    result = entity_correction_service.merge(database, alphabet.id, [holdings.id])

    assert [each.entity.id for each in similar] == [holdings.id] and similar[0].score > 0.3
    assert result.processed == 1
    assert entity_named(database, "Alphabet Holdings").merged_into_id == alphabet.id
    assert entity_named(database, "Waymo").parent_id == alphabet.id
    assert {alias[1] for alias in aliases_of(database, alphabet.id)} == {"Alphabet", "Alphabet Holdings"}
    assert entity_service.detail(database, alphabet.id).entity.report_count == 1
    assert [item.id for item in entity_service.overview(database, "company").entities] == [alphabet.id]
    assert entity_service.detail(database, holdings.id).merged_into.id == alphabet.id
    assert entity_correction_service.similar_entities(database, holdings.id) == []


def test_only_entities_of_one_kind_merge(database):
    extracted(database, "a", actors=[("Jordan", "state", "q"), ("Michael Jordan", "athlete", "q")])
    recognise_all(database, Ollama(recognition(item(1, "country", "Jordan", country_code="JO"), item(2, "person", "Michael Jordan"))))
    country, person = entity_named(database, "Jordan"), entity_named(database, "Michael Jordan")

    result = entity_correction_service.merge(database, country.id, [person.id])

    assert result.blocked == ["Michael Jordan (Person)"] and entity_named(database, "Michael Jordan").merged_into_id is None


def test_the_event_page_lists_the_entities_its_reports_name(database):
    _, candidate_id = extracted(database, "a", actors=[("Vladimir Zelensky", "president", "q")], impacts=[("Ukraine", "q")])
    recognise_all(database, Ollama(recognition(item(1, "person", "Volodymyr Zelenskyy"), item(2, "country", "Ukraine", country_code="UA"))))
    # The only report founds its event by the scores alone: the LLM is never asked.
    assign_all(database, Ollama(status=503))
    [candidate] = rows(database, EventCandidate, EventCandidate.id == candidate_id)

    found = entity_service.of_event(database, candidate.event_id)

    assert [(each.name, each.kind_label, each.report_count, each.uncertain, each.forms) for each in found] == [
        ("Ukraine", "Land", 1, False, ["Ukraine"]), ("Volodymyr Zelenskyy", "Person", 1, False, ["Vladimir Zelensky"]),
    ]


def test_the_list_sorts_by_name_with_umlauts_and_filters_by_kind_uncertain_and_name(database):
    extracted(database, "fruit", actors=[(name, "", "q") for name in ("birne", "Apfel", "Ärger", "Zitrone")])
    recognise_all(database, Ollama(recognition(*(item(number, "company", name) for number, name in enumerate(("birne", "Apfel", "Ärger", "Zitrone"), 1)))))
    two_zelenskys(database)

    everything = entity_service.overview(database)
    companies = entity_service.overview(database, "company", search="PFEL")
    uncertain = entity_service.overview(database, uncertain_only=True)

    assert [each.name for each in everything.entities] == ["Apfel", "Ärger", "birne", "Volodymyr Zelenskyy", "Zitrone"]
    assert ([each.name for each in companies.entities], companies.total) == (["Apfel"], 1)
    assert [(each.name, each.uncertain_count) for each in uncertain.entities] == [("Volodymyr Zelenskyy", 1)]
    assert everything.waiting == 0
