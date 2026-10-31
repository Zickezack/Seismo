"""Raw text lifecycle (userstories.md, Verdichtung und Daten-Lifecycle, story 1; datamodel.md, rule 8): a raw text -
the answer as fetched and its plain text - is deleted once its source's retention period is over since its article's
processing ended, and only then; the article keeps its metadata, its content hash and text fingerprint, and its
reports their quotes, so events stay verifiable without it (requirements.md, "Data Sources and Data Rights", item 3).

Processing ends with the article's current extraction, for a duplicate with its normalization, for any other article
once the extraction can take it no more (its maximum age). A text no reader understood, or one the model gave only
unusable answers for, keeps its raw text: the admin can have it processed again (Jobs), and it follows the rule then.
Once deleted, an article can no longer be extracted again (reprocessing_service) - the retention period, set per
Quelle, is how long that stays possible.

The worker deletes the due raw texts once an hour, in batches, each in a transaction of its own.
"""
import logging
from dataclasses import dataclass

from sqlalchemy import Engine

from app.repositories import raw_text_repository
from app.services import platform_settings_service
from app.services.source_service import DEFAULT_RAW_TEXT_RETENTION_DAYS

logger = logging.getLogger(__name__)

# Raw texts deleted per transaction, and at most per run; the rest follow an hour later.
DELETE_BATCH = 500
DELETE_LIMIT = 20_000


@dataclass(frozen=True)
class Lifecycle:
    """Where the raw texts stand (UI: Datenbestand, Speicher)."""

    processing: int  # processing goes on: normalization, or the extraction may still take it
    kept: int  # processed, kept for its source's retention period
    due: int  # its retention period over: deleted within the hour
    failed: int  # kept because its processing failed
    deleted: int  # deleted so far


def delete_due(engine: Engine) -> int:
    """Delete the raw texts whose retention period is over since their processing ended; how many."""
    deleted = 0
    while deleted < DELETE_LIMIT:
        with engine.begin() as connection:
            max_age = platform_settings_service.load(connection).extraction_max_age_days
            batch = raw_text_repository.delete_due(connection, max_age, DEFAULT_RAW_TEXT_RETENTION_DAYS, DELETE_BATCH)
        deleted += len(batch)
        if len(batch) < DELETE_BATCH:
            break
    if deleted:
        # A count only (datamodel.md, rule 13).
        logger.info("Raw texts deleted", extra={"raw_texts": deleted})
    return deleted


def lifecycle(engine: Engine) -> Lifecycle:
    with engine.connect() as connection:
        max_age = platform_settings_service.load(connection).extraction_max_age_days
        return Lifecycle(**raw_text_repository.lifecycle_counts(connection, max_age, DEFAULT_RAW_TEXT_RETENTION_DAYS))
