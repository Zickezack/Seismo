"""SQLAlchemy models. Documented in datamodel.md, "Entities"; changed only via skill datenmodell-aendern."""
import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class FullTextStatus(StrEnum):
    PENDING = "pending"
    STORED = "stored"
    DISALLOWED = "disallowed"  # by the host's robots.txt
    UNSUPPORTED = "unsupported"  # a content type the raw-data layer does not keep
    FAILED = "failed"
    BLOCKED = "blocked"  # the host refused it (HTTP 401, 403), often a bot protection
    RESERVED = "reserved"  # the publisher reserves text and data mining (TDMRep, or the host list); nothing kept
    SKIPPED = "skipped"  # source inactive or deleted, or fetched before full texts existed


class NormalizationStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"  # the text could not be read; normalization_error says why


class PublishedAtSource(StrEnum):
    FEED = "feed"  # as the feed states it
    GDELT = "gdelt"  # when GDELT first saw the article (seendate)
    PAGE = "page"  # the page's own full timestamp (article:published_time, JSON-LD datePublished)


class DuplicateReason(StrEnum):
    URL = "url"  # the same canonical URL
    TEXT = "text"  # the same cleaned text, on another host


class EventType(StrEnum):
    """What an event candidate is about (event schema, app/llm/event_schema.py); labels in extraction_service."""

    ARMED_CONFLICT = "armed_conflict"
    TERRORISM = "terrorism"
    CIVIL_UNREST = "civil_unrest"
    NATURAL_DISASTER = "natural_disaster"
    ACCIDENT = "accident"
    CYBER_INCIDENT = "cyber_incident"
    INFRASTRUCTURE_OUTAGE = "infrastructure_outage"
    SUPPLY_CHAIN_DISRUPTION = "supply_chain_disruption"
    ENERGY_SUPPLY = "energy_supply"
    COMMODITY_MARKET = "commodity_market"
    MONETARY_POLICY = "monetary_policy"
    FISCAL_POLICY = "fiscal_policy"
    TRADE_POLICY = "trade_policy"  # tariffs, sanctions, export controls
    REGULATION = "regulation"
    ELECTION_POLITICS = "election_politics"
    DIPLOMACY = "diplomacy"
    CORPORATE = "corporate"
    LABOR = "labor"
    HEALTH = "health"
    LEGAL = "legal"
    ECONOMIC_DATA = "economic_data"
    OTHER = "other"


class EventStatus(StrEnum):
    """The state an article reports, as the states of an event (Ereigniszustand, datamodel.md)."""

    START = "start"
    UPDATE = "update"
    ESCALATION = "escalation"
    CLOSURE = "closure"
    RESUMPTION = "resumption"
    UNKNOWN = "unknown"


class StatementKind(StrEnum):
    """Model-wide rule 6 (datamodel.md): Tatsache, Behauptung, Quellenprognose, Interpretation."""

    FACT = "fact"
    CLAIM = "claim"
    SOURCE_FORECAST = "source_forecast"  # a forecast made in the source, never one of the platform
    INTERPRETATION = "interpretation"


class ConfidenceLevel(StrEnum):
    """How sure: of an extraction, an inference, or a source's quality (datamodel.md, rule 7 - never combined)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ValidationResult(StrEnum):
    """What the check of a model's answer found (event schema, app/llm/event_schema.py)."""

    VALID = "valid"
    INVALID = "invalid"  # outside the schema
    INCOMPLETE = "incomplete"  # cut before its end


class BackfillStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"  # by the admin, or the source was deactivated


class TdmrepStatus(StrEnum):
    RULES = "rules"  # tdmrep_rules holds the rules of a valid file
    NONE = "none"  # no file, or none that could be read: the host does not use the protocol


class RobotsStatus(StrEnum):
    RULES = "rules"  # robots_txt holds the rules
    ALLOW_ALL = "allow_all"  # no robots.txt (404 and other 4xx)
    DISALLOW_ALL = "disallow_all"  # robots.txt answered 401 or 403


# A job with a dedupe key exists only once while it is waiting or running.
ACTIVE_JOB_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)
# Job.priority: a worker claims due jobs with a lower number first.
LIVE_PRIORITY = 0
BACKFILL_PRIORITY = 1


class Job(Base):
    """A unit of work in the PostgreSQL job queue (UI term: Job)."""

    __tablename__ = "job"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(100))
    # IDs only, never names or texts (datamodel.md, rule 13).
    payload: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), server_default=JobStatus.QUEUED.value)
    attempts: Mapped[int] = mapped_column(server_default="0")
    max_attempts: Mapped[int] = mapped_column(server_default="5")
    # Claimed in this order, then by run_after: live work (0) before backfill (1).
    priority: Mapped[int] = mapped_column(SmallInteger, server_default=str(LIVE_PRIORITY))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Set on every claim; a worker may only write to the job while its token is still the current one.
    lease_token: Mapped[uuid.UUID | None]
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(status.in_([s.value for s in JobStatus]), name="status"),
        CheckConstraint(attempts >= 0, name="attempts"),
        CheckConstraint(max_attempts >= 1, name="max_attempts"),
        CheckConstraint(priority >= 0, name="priority"),
        # The claim's order (job_repository.claim_next).
        Index("ix_job_status_priority_run_after", status, priority, run_after),
        Index(
            "uq_job_dedupe_key_active",
            dedupe_key,
            unique=True,
            postgresql_where=status.in_([s.value for s in ACTIVE_JOB_STATUSES]),
        ),
    )


class PlatformSettingsRecord(Base):
    """Plattform-Einstellungen: one row for the whole instance (requirements.md, "Settings")."""

    __tablename__ = "platform_settings"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False, server_default="1")
    llm_max_parallel_calls: Mapped[int] = mapped_column(server_default="1")
    succeeded_jobs_retention_days: Mapped[int] = mapped_column(server_default="7")
    failed_jobs_retention_days: Mapped[int] = mapped_column(server_default="30")
    backup_interval_hours: Mapped[int] = mapped_column(server_default="24")
    backups_to_keep: Mapped[int] = mapped_column(server_default="14")
    article_max_age_days: Mapped[int] = mapped_column(server_default="7")
    domain_min_interval_seconds: Mapped[int] = mapped_column(server_default="10")
    # Domains whose terms forbid text and data mining, one per line; NULL: the list the code starts with.
    tdm_blocked_domains: Mapped[str | None] = mapped_column(Text)
    # A quality per domain, "domain level" per line (extraction_service); NULL: the suggestions the code starts with.
    source_quality_domains: Mapped[str | None] = mapped_column(Text)
    # The event extraction's pre-filter and budget (extraction_service).
    extraction_languages: Mapped[str] = mapped_column(String(200), server_default="de,en,es,fr,it,nl,pt")
    extraction_min_text_chars: Mapped[int] = mapped_column(server_default="500")
    extraction_max_age_days: Mapped[int] = mapped_column(server_default="3")
    extractions_per_hour: Mapped[int] = mapped_column(server_default="30")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(id == 1, name="single_row"),
        CheckConstraint(llm_max_parallel_calls >= 1, name="llm_max_parallel_calls"),
        CheckConstraint(succeeded_jobs_retention_days >= 1, name="succeeded_jobs_retention_days"),
        CheckConstraint(failed_jobs_retention_days >= 1, name="failed_jobs_retention_days"),
        CheckConstraint(backup_interval_hours >= 1, name="backup_interval_hours"),
        CheckConstraint(backups_to_keep >= 1, name="backups_to_keep"),
        CheckConstraint(article_max_age_days >= 1, name="article_max_age_days"),
        CheckConstraint(domain_min_interval_seconds >= 1, name="domain_min_interval_seconds"),
        CheckConstraint(extraction_min_text_chars >= 0, name="extraction_min_text_chars"),
        CheckConstraint(extraction_max_age_days >= 1, name="extraction_max_age_days"),
        # 0 holds the extraction.
        CheckConstraint(extractions_per_hour >= 0, name="extractions_per_hour"),
    )


class Source(Base):
    """A news source (UI term: Quelle) with its adapter type and fetch rules."""

    __tablename__ = "source"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # Which adapter fetches it (app/sources/); checked in the service, so a new adapter needs no migration.
    adapter_type: Mapped[str] = mapped_column(String(20))
    url: Mapped[str] = mapped_column(String(2000))
    # The search of an adapter that needs one (GDELT), in that adapter's syntax; None for the others.
    query: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(server_default=true())
    # Its articles pass the event extraction's pre-filter (extraction_service).
    extract_events: Mapped[bool] = mapped_column(server_default=true())
    fetch_interval_minutes: Mapped[int] = mapped_column(server_default="60")
    # ISO 639-1 code; None for a source in several or unknown languages (datamodel.md, rule 5).
    language: Mapped[str | None] = mapped_column(String(10))
    # What it reports on: a continent, the Middle East or worldwide; checked in the service like the type.
    # None until set: sources from before regions existed.
    region: Mapped[str | None] = mapped_column(String(20))
    # Its articles' source quality where no domain of the list matches (extraction_service); None: unknown.
    quality: Mapped[str | None] = mapped_column(String(10))
    terms_note: Mapped[str | None] = mapped_column(Text)
    raw_text_retention_days: Mapped[int] = mapped_column(server_default="30")
    # Raises the platform's minimum interval for every request made for this source; None: the platform's.
    min_request_interval_seconds: Mapped[int | None] = mapped_column()
    # When the worker queues the next fetch; empty or past means due now (app/services/ingestion_service.py).
    next_fetch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The last failed fetch; kept after a success, while consecutive_failures goes back to 0.
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(fetch_interval_minutes >= 1, name="fetch_interval_minutes"),
        CheckConstraint(raw_text_retention_days >= 0, name="raw_text_retention_days"),
        CheckConstraint(min_request_interval_seconds >= 1, name="min_request_interval_seconds"),
        CheckConstraint(quality.in_([s.value for s in ConfidenceLevel]), name="quality"),
        # Names are unique regardless of case: "EZB" and "ezb" would be the same source to the admin.
        Index("uq_source_name", func.lower(name), unique=True),
    )


class Backfill(Base):
    """A period of a source fetched after the fact (UI term: Backfill), behind live work (datamodel.md)."""

    __tablename__ = "backfill"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"), index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Everything from here to period_end is fetched; the next request ends here. Moves back towards period_start.
    cursor: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), server_default=BackfillStatus.RUNNING.value)
    articles_found: Mapped[int] = mapped_column(server_default="0")
    articles_new: Mapped[int] = mapped_column(server_default="0")
    # Shown to the admin, never logged (datamodel.md, rule 13).
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(status.in_([s.value for s in BackfillStatus]), name="status"),
        CheckConstraint(period_start < period_end, name="period"),
    )


class SeedLogEntry(Base):
    """A seed item imported once (architecture.md, "Seed Data"), whatever became of its record since."""

    __tablename__ = "seed_log"

    seed_source: Mapped[str] = mapped_column(String(100), primary_key=True)
    item_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Article(Base):
    """One fetched report (UI term: Artikel). Its metadata is kept permanently (datamodel.md)."""

    __tablename__ = "article"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Empty once the source is deleted; source_name and source_url keep what it was at fetch time.
    source_id: Mapped[int | None] = mapped_column(ForeignKey("source.id", ondelete="SET NULL"))
    source_name: Mapped[str] = mapped_column(String(200))
    source_url: Mapped[str] = mapped_column(String(2000))
    # The item's own id within its source, else its link; repeated fetches find it again by this key.
    item_key: Mapped[str] = mapped_column(String(1000))
    url: Mapped[str] = mapped_column(String(2000))
    title: Mapped[str | None] = mapped_column(Text)
    # As the source states it, in UTC; empty when it states none (datamodel.md, rules 4 and 5).
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    language: Mapped[str | None] = mapped_column(String(10))
    full_text_status: Mapped[str] = mapped_column(String(20), server_default=FullTextStatus.PENDING.value)
    # Why the full text is not stored: shown to the admin, never logged (datamodel.md, rule 13).
    full_text_error: Mapped[str | None] = mapped_column(Text)
    # The backfill that found it; empty for live articles and once the backfill record is gone.
    backfill_id: Mapped[int | None] = mapped_column(ForeignKey("backfill.id", ondelete="SET NULL"), index=True)
    # The link without tracking parameters, then the page's own canonical link; the full text is fetched from it.
    canonical_url: Mapped[str | None] = mapped_column(String(2000))
    published_at_source: Mapped[str | None] = mapped_column(String(10))
    # Recognised in the text when sure enough; `language` stays as the source states it.
    detected_language: Mapped[str | None] = mapped_column(String(10))
    normalization_status: Mapped[str] = mapped_column(String(20), server_default=NormalizationStatus.PENDING.value)
    # Shown to the admin, never logged (datamodel.md, rule 13).
    normalization_error: Mapped[str | None] = mapped_column(Text)
    normalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # SHA-256 of the cleaned text from 200 characters on: kept with the metadata, after the raw text is gone.
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    # The original it duplicates, the article published first (duplicate_service); empty for an original.
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("article.id", ondelete="SET NULL"), index=True)
    duplicate_reason: Mapped[str | None] = mapped_column(String(10))
    # MinHash of the cleaned text from 200 characters on (similarity_service): kept with the metadata.
    text_signature: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    # Its bands: an article sharing one is a candidate for a similar text.
    text_bands: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))

    __table_args__ = (
        UniqueConstraint(source_id, item_key),
        Index("ix_article_fetched_at", fetched_at),
        CheckConstraint(full_text_status.in_([s.value for s in FullTextStatus]), name="full_text_status"),
        CheckConstraint(normalization_status.in_([s.value for s in NormalizationStatus]), name="normalization_status"),
        CheckConstraint(published_at_source.in_([s.value for s in PublishedAtSource]), name="published_at_source"),
        CheckConstraint(duplicate_reason.in_([s.value for s in DuplicateReason]), name="duplicate_reason"),
        # Equality only: a B-tree's key size limit would refuse the longest links.
        Index("ix_article_canonical_url", canonical_url, postgresql_using="hash"),
        # The candidates for a similar text: articles whose bands overlap (`&&`).
        Index("ix_article_text_bands", text_bands, postgresql_using="gin"),
        # The worker's sweep for articles to normalize (normalization_service.enqueue_due).
        Index(
            "ix_article_normalization_pending",
            "id",
            postgresql_where=normalization_status == NormalizationStatus.PENDING.value,
        ),
    )


class ArticleLink(Base):
    """Two articles with similar texts (UI: Ähnliche Artikel), or a pair the admin separated (datamodel.md)."""

    __tablename__ = "article_link"

    # The lower id first: a pair has one row.
    article_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("article.id", ondelete="CASCADE"), primary_key=True)
    other_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("article.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    # The estimated share of common word 5-grams; empty for a pair separated without a link ("Kein Duplikat").
    similarity: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Set by the admin: the two are unrelated, never linked again and never one a duplicate of the other.
    separated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (CheckConstraint(article_id < other_id, name="ordered_pair"),)


class Extraction(Base):
    """One LLM run on an article (UI term: Extraktion): with what it ran, on how much of the text (datamodel.md)."""

    __tablename__ = "extraction"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    article_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("article.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(String(200))  # as Ollama names it
    schema_version: Mapped[str] = mapped_column(String(20))
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    duration_ms: Mapped[int]
    input_chars: Mapped[int]  # of the text the model saw
    # The text was cut to fit the model's context.
    truncated: Mapped[bool]
    # Statements whose quote is not in the text: not stored.
    statements_dropped: Mapped[int] = mapped_column(server_default="0")
    validation_result: Mapped[str] = mapped_column(String(20), server_default=ValidationResult.VALID.value)
    # Of an answer outside the schema or cut: what was wrong (field and kind of error, never its value) and the
    # answer itself, for diagnosis. Shown to the admin, never logged (datamodel.md, rule 13).
    validation_errors: Mapped[str | None] = mapped_column(Text)
    raw_answer: Mapped[str | None] = mapped_column(Text)
    # Valid only after the model was asked again with the errors of its first answer.
    repaired: Mapped[bool] = mapped_column(server_default=false())

    __table_args__ = (
        CheckConstraint(validation_result.in_([s.value for s in ValidationResult]), name="validation_result"),
    )


class EventCandidate(Base):
    """An event as one article reports it (UI term: Ereigniskandidat), before it is assigned to an event."""

    __tablename__ = "event_candidate"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    extraction_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("extraction.id", ondelete="CASCADE"), index=True)
    article_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("article.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(SmallInteger)  # in the model's answer
    event_type: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(Text)  # in English, like every text the model writes
    summary: Mapped[str] = mapped_column(Text)
    # Empty where the article does not state it (datamodel.md, rule 5).
    country: Mapped[str | None] = mapped_column(String(2))  # ISO 3166-1 alpha-2
    place: Mapped[str | None] = mapped_column(Text)
    # The passage that states place and country; without one found, both are unknown (schema 2).
    location_quote: Mapped[str | None] = mapped_column(Text)
    # The time as the article states it, e.g. "on Friday morning": a model's own date proved a guess.
    time_note: Mapped[str | None] = mapped_column(Text)
    time_quote: Mapped[str | None] = mapped_column(Text)
    # How sure type, status and impacts are derived, as the model rates it; None before schema 2.
    inference_confidence: Mapped[str | None] = mapped_column(String(10))
    actors: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    impacts: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))

    __table_args__ = (
        CheckConstraint(event_type.in_([s.value for s in EventType]), name="event_type"),
        CheckConstraint(status.in_([s.value for s in EventStatus]), name="status"),
        CheckConstraint(inference_confidence.in_([s.value for s in ConfidenceLevel]), name="inference_confidence"),
    )


class Statement(Base):
    """One statement of an event candidate (UI term: Aussage), with its kind (datamodel.md, rule 6)."""

    __tablename__ = "statement"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    candidate_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("event_candidate.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(SmallInteger)
    kind: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    # How faithfully the statement renders its quote, as the model rates it, lowered for a cut text or a repaired
    # answer; None before schema 2.
    extraction_confidence: Mapped[str | None] = mapped_column(String(10))

    __table_args__ = (
        CheckConstraint(kind.in_([s.value for s in StatementKind]), name="kind"),
        CheckConstraint(extraction_confidence.in_([s.value for s in ConfidenceLevel]), name="extraction_confidence"),
    )


class Evidence(Base):
    """The quote that backs a statement (UI term: Beleg): kept with the metadata after the raw text is gone (rule 8)."""

    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    statement_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("statement.id", ondelete="CASCADE"), index=True)
    article_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("article.id", ondelete="CASCADE"), index=True)
    quote: Mapped[str] = mapped_column(Text)  # verbatim from the article's cleaned text
    # The quality of the source the quote is from, as it stood at extraction; None: unknown.
    source_quality: Mapped[str | None] = mapped_column(String(10))

    __table_args__ = (CheckConstraint(source_quality.in_([s.value for s in ConfidenceLevel]), name="source_quality"),)


class RawText(Base):
    """An article's content as fetched (UI term: Rohtext), in the temporary raw-data layer (datamodel.md, rule 8)."""

    __tablename__ = "raw_text"

    article_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("article.id", ondelete="CASCADE"), primary_key=True)
    # The bytes as received: the normalisation reads the encoding from content_type and the document itself.
    content: Mapped[bytes] = mapped_column(LargeBinary)
    content_type: Mapped[str | None] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(2000))  # after redirects
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The main text, cleaned (normalization_service): full text as well, so it shares the raw text's lifecycle.
    plain_text: Mapped[str | None] = mapped_column(Text)


class FetchHost(Base):
    """What every fetch from one host goes through: its robots.txt and its request slots (not shown in the UI)."""

    __tablename__ = "fetch_host"

    host: Mapped[str] = mapped_column(String(255), primary_key=True)  # lower case, with a port other than the default
    # Start of the next free request slot; each reservation moves it one interval ahead.
    next_slot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Until when the host asked for a pause (Retry-After); no request goes there before.
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    robots_status: Mapped[str | None] = mapped_column(String(20))
    robots_txt: Mapped[str | None] = mapped_column(Text)
    robots_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The host's /.well-known/tdmrep.json (TDM Reservation Protocol): its rules while it has a valid one.
    tdmrep_status: Mapped[str | None] = mapped_column(String(20))
    tdmrep_rules: Mapped[str | None] = mapped_column(Text)
    tdmrep_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(robots_status.in_([s.value for s in RobotsStatus]), name="robots_status"),
        CheckConstraint(tdmrep_status.in_([s.value for s in TdmrepStatus]), name="tdmrep_status"),
    )
