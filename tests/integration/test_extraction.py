"""Event extraction in PostgreSQL: which articles pass the pre-filter and the hour's budget, and what an extraction job
stores - against a simulated Ollama."""
import json
import threading
from datetime import timedelta
from functools import partial
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import select, update

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
    """Answers every chat with `content`, or with `status`; records what it was asked."""

    def __init__(self, content=None, status=200, done_reason="stop"):
        self.content, self.status, self.done_reason, self.requests = content, status, done_reason, []

    def __call__(self, request):
        self.requests.append(json.loads(request.content))
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": "model 'qwen3.5:9b' not found"})
        body = {"model": "qwen3.5:9b", "message": {"role": "assistant", "content": self.content}, "done_reason": self.done_reason}
        return httpx2.Response(200, json=body | {"prompt_eval_count": 900, "eval_count": 300})


def answer(*quotes):
    statements = [{"kind": kind, "text": f"Statement {number}.", "quote": quote} for number, (kind, quote) in enumerate(quotes)]
    return json.dumps({"events": [{
        "event_type": "monetary_policy", "status": "update", "title": "ECB keeps rates unchanged",
        "summary": "The ECB left its key rates unchanged.", "location": {"country": None, "place": "euro area"},
        "time_note": "on Thursday", "actors": [{"name": "ECB", "role": "central bank"}],
        "impacts": [], "statements": statements,
    }]})


def extract_all(database, ollama):
    llm = partial(OllamaClient, "http://ollama:11434", "qwen3.5:9b", 8192, transport=httpx2.MockTransport(ollama))
    stop = threading.Event()
    handlers = build_handlers(database, Path("/nonexistent"), LlmCallLimiter(database, stop), stop, "ops@example.org", llm)
    handlers = {KIND: handlers[KIND]}
    while run_next_job(database, handlers, "test", 10, on_renewed=lambda: None):
        pass


@pytest.fixture
def queued(database):
    article_id = add(database, source(database), "a")
    normalize_all(database)
    extraction_service.enqueue_due(database)
    return article_id


def test_an_extraction_stores_its_candidates_with_the_quotes_found_in_the_text(database, queued):
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
            select(Statement.kind, Evidence.quote, Evidence.article_id).join(Evidence).order_by(Statement.position)
        ).all()
    assert (extraction.article_id, extraction.model, extraction.schema_version) == (queued, "qwen3.5:9b", "1")
    assert (extraction.truncated, extraction.statements_dropped, extraction.input_chars > 500) == (False, 1, True)
    assert (candidate.event_type, candidate.status, candidate.place, candidate.time_note) == ("monetary_policy", "update", "euro area", "on Thursday")
    assert candidate.actors == [{"name": "ECB", "role": "central bank"}]
    assert [tuple(row) for row in statements] == [
        ("fact", RATE_DECISION, queued), ("claim", "kündigte an, die Daten der kommenden Monate genau zu verfolgen", queued)
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


@pytest.mark.parametrize(
    ("ollama", "status", "error"),
    [
        (Ollama(status=400), JobStatus.FAILED, "PermanentJobError: Ollama: HTTP 400"),
        (Ollama(status=404), JobStatus.QUEUED, "LlmUnavailable: Ollama: HTTP 404: model 'qwen3.5:9b' not found"),
        (Ollama(status=503), JobStatus.QUEUED, "LlmUnavailable: Ollama: HTTP 503"),
        (Ollama('{"events": [{"event_type": "flood"}]}'), JobStatus.QUEUED, "ValidationError"),
        (Ollama('{"events": [', done_reason="length"), JobStatus.QUEUED, "IncompleteAnswer: Antwort nach 300 Tokens abgeschnitten"),
    ],
    ids=["refused", "model not pulled", "server failing", "outside the schema", "cut answer"],
)
def test_a_refused_request_fails_at_once_anything_else_is_retried(database, queued, ollama, status, error):
    extract_all(database, ollama)

    [job] = jobs(database, KIND)
    assert job.status == status and job.last_error.startswith(error)
    with database.connect() as connection:
        assert connection.execute(select(Extraction.id)).all() == []
