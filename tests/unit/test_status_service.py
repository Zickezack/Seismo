"""The status page's rules without a database (status_service): labels, the job kinds' figures over hours, the
bottleneck rules, and a recording that never fails its job."""
from datetime import UTC, datetime
from types import SimpleNamespace

from app.llm.ollama import LlmCallRecord
from app.services import status_service
from app.services.status_service import (
    KindFigures,
    QueueRow,
    _kind_figures,
    bottlenecks,
    chf_label,
    count_label,
    duration_label,
    share_label,
)


def test_durations_counts_shares_and_francs_read_as_on_the_pages():
    assert [duration_label(each) for each in (None, 3, 89, 270, 5400, 4 * 3600, 3 * 86400)] == [
        "–", "3 s", "89 s", "4,5 min", "1,5 h", "4,0 h", "3,0 d",
    ]
    assert duration_label(30 * 60) == "30 min"
    assert count_label(12345.6) == "12'346" and count_label(30.75, 1) == "30,8" and count_label(None) == "–"
    assert share_label(1, 3) == "33 %" and share_label(1, 0) == "–"
    assert chf_label(1.23) == "CHF 0.0123" and chf_label(1230) == "CHF 12.30"


def hour(kind, **values):
    base = {"kind": kind, "finished": 0, "failed": 0, "attempts": 0, "failed_attempts": 0, "started": 0, "wait_seconds": 0.0,
            "wait_seconds_max": 0.0, "run_seconds": 0.0, "run_seconds_max": 0.0, "queued_due": 0, "oldest_due_seconds": None}
    return base | values


def test_the_kinds_figures_add_the_hours_up_and_keep_the_longest():
    rows = [
        hour("normalize_article", finished=10, attempts=11, failed_attempts=1, started=10, wait_seconds=100, wait_seconds_max=30,
             run_seconds=20, run_seconds_max=5),
        hour("normalize_article", finished=30, failed=1, attempts=32, failed_attempts=3, started=30, wait_seconds=300,
             wait_seconds_max=60, run_seconds=60, run_seconds_max=4),
        hour("backup", queued_due=1),  # only queued: no figures
    ]
    [figures] = _kind_figures(rows)
    assert (figures.label, figures.finished, figures.failed, figures.attempts, figures.failed_attempts) == ("Artikel normalisieren", 40, 1, 43, 4)
    assert (figures.wait_mean, figures.wait_max, figures.run_mean, figures.run_max) == (10.0, 60.0, 2.0, 5.0)
    assert figures.failed_label == "9 %"


def kind(name, attempts, failed_attempts):
    return KindFigures(name, attempts, 0, attempts, failed_attempts, attempts, 1.0, 1.0, 1.0, 1.0)


def test_the_bottlenecks_are_named_by_fixed_rules():
    queue = [QueueRow("extract_events", 0, 4, 0, 1, 2 * 3600), QueueRow("fetch_article", 1, 9, 0, 0, 1800)]
    kinds = [kind("fetch_article", 100, 31), kind("normalize_article", 5, 5), kind("assign_event", 100, 10)]
    found = bottlenecks(0.92, 1, queue, kinds, 442, 30, 30)
    assert found == [
        "LLM ausgelastet: 92 % der Zeit von 1 Platz belegt.",
        "Warteschlange: der älteste fällige Job «Ereignisse extrahieren» wartet seit 2,0 h (4 fällig).",
        "Fehler: «Volltext abrufen» scheitert bei 31 % der Versuche (31 von 100).",
        "Extraktionsbudget ausgeschöpft: 442 Artikel warten (30 von 30 pro Stunde).",
    ]
    # Below every rule: too few attempts, 10 % exactly, a budget left, a short wait.
    assert bottlenecks(0.79, 2, queue[1:], kinds[1:], 442, 30, 29) == []
    assert bottlenecks(0.0, 1, [], [], 10_000, 30, 30) == ["Extraktionsbudget ausgeschöpft: über 10000 Artikel warten (30 von 30 pro Stunde)."]


def test_an_article_is_found_from_a_jobs_payload_where_it_names_one():
    assert status_service.article_of(None, "extract_events", {"article_id": 7}) == 7
    assert status_service.article_of(None, "assess_impacts", {"event_id": 3}) is None
    assert status_service.article_of(None, "recognise_entities", {}) is None


class Broken:
    def begin(self):
        raise RuntimeError("database away")


def test_a_call_not_recorded_is_only_logged(caplog):
    record = status_service.recorder(Broken(), SimpleNamespace(id=5, kind="extract_events", payload={"article_id": 7}))
    record(LlmCallRecord(datetime(2026, 11, 4, tzinfo=UTC), 1200, "qwen3.5:9b", 800, 90, "answered"))
    assert "LLM call not recorded" in caplog.text
