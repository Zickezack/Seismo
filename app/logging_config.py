"""Structured logs: one JSON object per line on stdout.

Logs reference entities and articles by ID only, never by name or text (datamodel.md, rule 13).
"""
import json
import logging
import sys
from datetime import UTC, datetime

# Attributes every LogRecord has; anything else was passed via `extra=` and is logged as a field.
# Uvicorn adds `color_message`, a copy of the message with terminal colour codes.
_STANDARD_ATTRIBUTES = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName", "color_message"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRIBUTES and key not in entry:
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(service: str, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Uvicorn installs its own handlers unless told otherwise; route its logs through ours.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    # The request middleware (app/web/middleware.py) already logs every request as JSON.
    logging.getLogger("uvicorn.access").disabled = True
