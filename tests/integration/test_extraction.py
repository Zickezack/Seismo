"""Event extraction in PostgreSQL: which articles pass the pre-filter and the hour's budget, and what an extraction job
stores - against a simulated Ollama."""
import json
import threading
from datetime import timedelta
from functools import partial
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import func, select, update

from app.db.models import Article, EventCandidate, Evidence, Extraction, Job, JobStatus, Statement
from app.jobs.handlers import build_handlers
from app.jobs.runner import run_next_job
from app.llm.limiter import LlmCallLimiter
from app.llm.ollama import OllamaClient
from app.repositories import extraction_repository
from app.services import extraction_service, job_service, platform_settings_service, source_service
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_ingestion import NOW, jobs
from tests.integration.test_normalization import article, normalize_all
from tests.integration.test_similarity import story
from tests.unit.test_normalization_service import GERMAN, page

KIND = job_service.EXTRACT_EVENTS_JOB_KIND
RATE_DECISION = "Die Europäische Zentralbank hat die Leitzinsen am Donnerstag unverändert gelassen."
MORE = (
    "Ökonomen hatten den Entscheid erwartet. An den Märkten blieb es ruhig: Der Euro notierte kaum verändert, und die "
    "Renditen deutscher Staatsanleihen gaben leicht nach. Mehrere Ratsmitglieder hatten zuvor betont, dass die Inflation "
    "zwar sinke, der Lohnanstieg aber noch zu hoch sei, um die Zinsen schon im Dezember zu senken."
)
REPORT = page(body=f"<article><h1>Leitzins</h1><p>{GERMAN}</p><p>{MORE}</p></article>")


def source(database, name="EZB", extract=True):
    form = {"name": name, "adapter_type": "rss", "url": f"https://{name.lower()}.example/rss", "is_active": "on",
            "fetch_interval_minutes": "60", "raw_text_retention_days": "30", "region": "europe"}
    return source_service.create(database, form | ({"extract_events": "on"} if extract else {})).id


def add(database, source_id, key, raw=None, host="bank.example", **fields):
    return article(database, source_id, key, REPORT if raw is None else raw, url=f"https://{host}/{key}.html", **fields)


def due(database):
    with database.connect() as connection:
        settings = platform_settings_service.load(connection)
        return extraction_repository.due(
            connection, KIND, settings.extraction_language_codes, settings.extraction_min_text_chars,
            settings.extraction_max_age_days, 100,
        )


def test_the_pre_filter_takes_originals_of_extracting_sources_with_enough_recent_text(database):
    on, off = source(database), source(database, "Fed", extract=False)
    passing = add(database, on, "a")
    add(database, off, "b")
    add(database, on, "short", page(body="<article><h1>Kurz</h1><p>Leitzins unverändert.</p></article>"))
    add(database, on, "old", published_at=NOW - timedelta(days=4))
    normalize_all(database)

    assert due(database) == [passing]


def test_the_language_is_the_text_s_else_the_one_the_source_states(database):
    german = add(database, source(database), "a")
    normalize_all(database)

    platform_settings_service.save(database, {"extraction_languages": "en, fr"})
    assert due(database) == []
    platform_settings_service.save(database, {"extraction_languages": "de"})
    assert due(database) == [german]
    with database.begin() as connection:
        connection.execute(update(Article).values(detected_language=None, language="fr"))
    platform_settings_service.save(database, {"extraction_languages": "fr"})
    assert due(database) == [german]


def test_a_duplicate_and_a_later_copy_of_a_linked_article_wait_for_their_original(database):
    source_id = source(database)
    original = add(database, source_id, "o", story(), "a.example", published_at=NOW - timedelta(hours=1))
    add(database, source_id, "d", story(), "b.example")
    add(database, source_id, "c", story(changed=4), "c.example")
    normalize_all(database)
    platform_settings_service.save(database, {"extraction_languages": "de,en"})

    assert due(database) == [original]


def test_the_budget_takes_the_newest_first_and_counts_the_hour(database):
    source_id = source(database)
    # One host: the same text under three links is neither duplicate nor linked (duplicate_service), just three articles.
    keys = [add(database, source_id, key, published_at=NOW - timedelta(hours=hours)) for key, hours in (("a", 3), ("b", 1), ("c", 2))]
    normalize_all(database)
    platform_settings_service.save(database, {"extractions_per_hour": "2"})

    extraction_service.enqueue_due(database)

    assert sorted(job.payload["article_id"] for job in jobs(database, KIND)) == sorted([keys[1], keys[2]])
    assert extraction_service.enqueue_due(database) == []
    # A queued or failed job keeps its article from being queued again; a new hour lets the rest in.
    with database.begin() as connection:
        connection.execute(update(Job).where(Job.kind == KIND).values(status=JobStatus.FAILED, created_at=NOW - timedelta(hours=2)))
    extraction_service.enqueue_due(database)
    assert sorted(job.payload["article_id"] for job in jobs(database, KIND)) == sorted(keys)
    platform_settings_service.save(database, {"extractions_per_hour": "0"})
    assert extraction_service.enqueue_due(database) == []


class Ollama:
    """Answers the chats in turn with `contents` - the last one again once they run out - or with `status`; a
    `(content, done_reason)` pair answers with that reason. Records what it was asked."""

    def __init__(self, *contents, status=200):
        self.contents, self.status, self.requests = list(contents), status, []

    def __call__(self, request):
        self.requests.append(json.loads(request.content))
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": "model 'qwen3.5:9b' not found"})
        content = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        content, done_reason = content if isinstance(content, tuple) else (content, "stop")
        body = {"model": "qwen3.5:9b", "message": {"role": "assistant", "content": content}, "done_reason": done_reason}
        return httpx2.Response(200, json=body | {"prompt_eval_count": 900, "eval_count": 300})


def answer(*quotes):
    statements = [{"kind": kind, "text": f"Statement {number}.", "quote": quote, "confidence": "high"}
                  for number, (kind, quote) in enumerate(quotes)]
    return json.dumps({"events": [{
        "event_type": "monetary_policy", "status": "update", "confidence": "medium", "title": "ECB keeps rates unchanged",
        "summary": "The ECB left its key rates unchanged.",
        "location": {"country": None, "place": "euro area", "quote": "Inflation im Euroraum"},
        "time_note": "on Thursday", "time_quote": "am Donnerstag unverändert",
        "actors": [{"name": "ECB", "role": "central bank", "quote": "Die Europäische Zentralbank"},
                   {"name": "IMF", "role": "observer", "quote": "Der Internationale Währungsfonds"}],
        "impacts": [{"target": "German bonds", "target_kind": "market", "direction": "positive", "horizon": "days",
                     "description": "Yields eased.", "quote": "die Renditen deutscher Staatsanleihen gaben leicht nach"}],
        "statements": statements,
    }]})


INVALID = '{"events": [{"event_type": "flood"}]}'


def extract_all(database, ollama, stop=None, jobs_to_run=None):
    """Run the due extraction jobs, or as many as `jobs_to_run`: a stopping worker claims no next one."""
    llm = partial(OllamaClient, "http://ollama:11434", "qwen3.5:9b", 8192, transport=httpx2.MockTransport(ollama))
    stop = stop or threading.Event()
    handlers = build_handlers(database, Path("/nonexistent"), LlmCallLimiter(database, stop), stop, "ops@example.org", llm)
    handlers = {KIND: handlers[KIND]}
    run = 0
    while (jobs_to_run is None or run < jobs_to_run) and run_next_job(database, handlers, "test", 10, on_renewed=lambda: None):
        run += 1


@pytest.fixture
def queued(database):
    article_id = add(database, source(database), "a")
    normalize_all(database)
    extraction_service.enqueue_due(database)
    return article_id


def test_an_extraction_stores_its_candidates_with_the_quotes_found_in_the_text(database, queued):
    platform_settings_service.save(database, {"source_quality_domains": "bank.example mittel"})
    ollama = Ollama(answer(
        ("fact", RATE_DECISION),
        ("claim", "kündigte an, die Daten der kommenden Monate genau zu verfolgen"),
        ("source_forecast", "Die Zinsen steigen im Dezember wieder"),
    ))

    extract_all(database, ollama)

    with database.connect() as connection:
        extraction = connection.execute(select(Extraction.__table__)).one()
        candidate = connection.execute(select(EventCandidate.__table__)).one()
        statements = connection.execute(
            select(Statement.kind, Statement.extraction_confidence, Evidence.quote, Evidence.source_quality, Evidence.article_id)
            .join(Evidence)
            .order_by(Statement.position)
        ).all()
    assert (extraction.article_id, extraction.model, extraction.schema_version) == (queued, "qwen3.5:9b", "2")
    assert (extraction.validation_result, extraction.repaired, extraction.validation_errors) == ("valid", False, None)
    assert (extraction.truncated, extraction.statements_dropped, extraction.input_chars > 500) == (False, 1, True)
    assert (candidate.event_type, candidate.status, candidate.inference_confidence) == ("monetary_policy", "update", "medium")
    assert (candidate.place, candidate.location_quote) == ("euro area", "Inflation im Euroraum")
    assert (candidate.time_note, candidate.time_quote) == ("on Thursday", "am Donnerstag unverändert")
    # The IMF is not in the text: left out, not invented.
    assert candidate.actors == [{"name": "ECB", "role": "central bank", "quote": "Die Europäische Zentralbank"}]
    assert candidate.impacts[0]["quote"] == "die Renditen deutscher Staatsanleihen gaben leicht nach"
    assert [tuple(row) for row in statements] == [
        ("fact", "high", RATE_DECISION, "medium", queued),
        ("claim", "high", "kündigte an, die Daten der kommenden Monate genau zu verfolgen", "medium", queued),
    ]
    [request] = ollama.requests
    assert RATE_DECISION in request["messages"][1]["content"] and request["messages"][1]["content"].startswith("Source: EZB")
    view = extraction_service.latest_for_article(database, queued)
    assert [statement.kind_label for statement in view.candidates[0].statements] == ["Tatsache", "Behauptung"]
    # Done once: the article is not queued again.
    assert extraction_service.enqueue_due(database) == []


def test_an_article_that_became_a_duplicate_meanwhile_is_not_extracted(database, queued):
    copy_of = add(database, source(database, "Agentur"), "o", host="agency.example", published_at=NOW - timedelta(hours=1))
    with database.begin() as connection:
        connection.execute(update(Article).where(Article.id == queued).values(duplicate_of_id=copy_of, duplicate_reason="text"))
    ollama = Ollama(answer(("fact", RATE_DECISION)))

    extract_all(database, ollama)

    assert ollama.requests == [] and [job.status for job in jobs(database, KIND)] == [JobStatus.SUCCEEDED]


def runs(database):
    with database.connect() as connection:
        return connection.execute(
            select(Extraction.validation_result, Extraction.repaired, Extraction.validation_errors, Extraction.raw_answer)
            .order_by(Extraction.id)
        ).all()


@pytest.mark.parametrize("first", [INVALID, ('{"events": [', "length")], ids=["outside the schema", "cut"])
def test_an_unusable_answer_is_stored_and_asked_again_valid_then_with_every_level_one_lower(database, queued, first):
    ollama = Ollama(first, answer(("fact", RATE_DECISION)))

    extract_all(database, ollama)

    (result, repaired, errors, raw), valid = runs(database)
    assert (result, repaired, valid.validation_result, valid.repaired, valid.raw_answer) == (
        "invalid" if first == INVALID else "incomplete", False, "valid", True, None
    )
    assert raw == (first if first == INVALID else first[0]) and errors
    follow_up = ollama.requests[1]["messages"][1]["content"]
    assert "A previous answer was not usable" in follow_up and first[0] not in follow_up.split("A previous answer")[1]
    with database.connect() as connection:
        assert connection.execute(select(Statement.extraction_confidence)).scalar_one() == "medium"
    assert [job.status for job in jobs(database, KIND)] == [JobStatus.SUCCEEDED]


@pytest.mark.parametrize(
    ("ollama", "status", "error"),
    [
        (Ollama(status=400), JobStatus.FAILED, "PermanentJobError: Ollama: HTTP 400"),
        (Ollama(INVALID), JobStatus.QUEUED, "InvalidAnswer: Antwort auch auf Nachfrage ausserhalb des Schemas: events.0.event_type"),
        (Ollama(('{"events": [', "length")), JobStatus.QUEUED, "InvalidAnswer: Antwort auch auf Nachfrage abgeschnitten: cut off after 300 tokens"),
    ],
    ids=["refused", "outside the schema", "cut answer"],
)
def test_a_refused_request_fails_at_once_anything_else_is_retried(database, queued, ollama, status, error):
    extract_all(database, ollama)

    [job] = jobs(database, KIND)
    assert job.status == status and job.last_error.startswith(error)
    assert extraction_service.latest_for_article(database, queued) is None


def test_two_unusable_answers_are_both_stored_and_the_job_retried_later(database, queued):
    extract_all(database, Ollama(INVALID))

    assert [(result, repaired) for result, repaired, _errors, _raw in runs(database)] == [("invalid", False), ("invalid", True)]
    [job] = jobs(database, KIND)
    assert (job.status, job.attempts) == (JobStatus.QUEUED, 1)
    # Its job is there: the sweep does not queue the article a second time.
    assert extraction_service.enqueue_due(database) == []


def test_a_stopping_worker_asks_no_follow_up_question_and_puts_the_job_back(database, queued):
    ollama = Ollama(INVALID, answer(("fact", RATE_DECISION)))
    stopping = threading.Event()
    stopping.set()

    extract_all(database, ollama, stopping, jobs_to_run=1)

    assert len(ollama.requests) == 1
    assert [(result, repaired) for result, repaired, _errors, _raw in runs(database)] == [("invalid", False)]
    [job] = jobs(database, KIND)
    # Back into the queue, the attempt not counted (app/jobs/errors.py, JobInterrupted).
    assert (job.status, job.attempts) == (JobStatus.QUEUED, 0)


def refused(request):
    raise httpx2.ConnectError("refused", request=request)


@pytest.mark.parametrize("away", [refused, Ollama(status=503), Ollama(status=404)], ids=["no connection", "server failing", "model not pulled"])
def test_a_job_that_finds_ollama_away_waits_without_using_an_attempt(database, queued, away):
    extract_all(database, away, jobs_to_run=1)

    [job] = jobs(database, KIND)
    assert (job.status, job.attempts, job.last_error) == (JobStatus.QUEUED, 0, None)
    with database.connect() as connection:
        delay = connection.execute(select(Job.run_after - func.now()).where(Job.id == job.id)).scalar_one()
    assert timedelta(seconds=50) < delay <= timedelta(seconds=60)
