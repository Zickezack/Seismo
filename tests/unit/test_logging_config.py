import json
import logging
import sys

import pytest

from app.logging_config import JsonFormatter, configure_logging


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.getLogger("uvicorn.access").disabled = False


def test_configure_logging_writes_json_and_silences_the_duplicate_access_log(restore_logging, capsys):
    configure_logging("api", logging.INFO)

    logging.getLogger("uvicorn.error").info("Started server process")
    logging.getLogger("uvicorn.access").info("127.0.0.1 - GET /health/live 200")

    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["message"] for line in lines] == ["Started server process"]


def make_record(message="Article fetched", level=logging.INFO, exc_info=None, **extra):
    record = logging.LogRecord("app.test", level, __file__, 1, message, None, exc_info)
    record.__dict__.update(extra)
    return record


def test_formats_one_json_object_with_the_standard_fields():
    entry = json.loads(JsonFormatter("api").format(make_record()))

    assert entry["level"] == "INFO"
    assert entry["service"] == "api"
    assert entry["logger"] == "app.test"
    assert entry["message"] == "Article fetched"
    assert entry["time"].endswith("+00:00")


def test_extra_fields_become_json_fields():
    entry = json.loads(JsonFormatter("worker").format(make_record(article_id=42, status=200)))

    assert entry["article_id"] == 42
    assert entry["status"] == 200


def test_uvicorn_colour_copy_of_the_message_is_dropped():
    entry = json.loads(JsonFormatter("api").format(make_record(color_message="\x1b[36mStarted\x1b[0m")))

    assert "color_message" not in entry


def test_extra_fields_never_override_the_standard_fields():
    entry = json.loads(JsonFormatter("worker").format(make_record(service="other")))

    assert entry["service"] == "worker"


def test_values_json_cannot_encode_are_written_as_text():
    entry = json.loads(JsonFormatter("api").format(make_record(payload=object())))

    assert entry["payload"].startswith("<object object")


def test_exception_is_included():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = make_record("Failed", logging.ERROR, exc_info=sys.exc_info())

    entry = json.loads(JsonFormatter("api").format(record))

    assert "RuntimeError: boom" in entry["exception"]
