"""SQLAlchemy models. Documented in datamodel.md, "Entities"; changed only via skill datenmodell-aendern."""
import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
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


class AssignmentMethod(StrEnum):
    """How an event candidate came to its event (event_service)."""

    NEW = "new"  # nothing alike: it founded the event
    SCORE = "score"  # similar enough by its words
    LLM = "llm"  # in between: the LLM judged it the same incident


class CorrectionKind(StrEnum):
    """What a correction of the grouping did (event_correction_service)."""

    MERGE = "merge"  # events merged into one
    SPLIT = "split"  # reports of an event split off into a new one


class EntityKind(StrEnum):
    """What an entity is (entity_service); labels there. Organisation: neither company nor authority - OPEC, a party,
    a union, an armed group (the admin's decision of 2026-10-31)."""

    COMPANY = "company"
    SUBSIDIARY = "subsidiary"
    PERSON = "person"  # in a public or business role only: personal data (datamodel.md, rule 13)
    COUNTRY = "country"
    AUTHORITY = "authority"
    ORGANIZATION = "organization"
    PRODUCT = "product"
    COMMODITY = "commodity"  # of the starter list only
    SECTOR = "sector"  # of the starter list only


class AliasScheme(StrEnum):
    """What a name form of an entity is: a name, or an identifier of a scheme."""

    NAME = "name"
    ISO3166 = "iso3166"  # a country's ISO 3166-1 alpha-2 code
    ISIN = "isin"
    LEI = "lei"
    TICKER = "ticker"  # EXCHANGE:SYMBOL
    CATALOGUE = "catalogue"  # the key of an item of the starter list of sectors and commodities


class AliasMatch(StrEnum):
    """How a name form came to its entity (entity_service)."""

    FOUNDING = "founding"  # the entity was recognised with it first
    NAME = "name"  # recognised together with a name form the entity has
    IDENTIFIER = "identifier"  # recognised together with an identifier the entity has
    LLM = "llm"  # a similar name the LLM judged the same: uncertain until the analyst confirms it
    SEED = "seed"  # the starter list
    SPLIT = "split"  # split off into an entity of its own by the analyst


class MentionSource(StrEnum):
    """Where in its report an entity is named."""

    ACTOR = "actor"
    IMPACT = "impact"  # the target of an impact


class RelationshipKind(StrEnum):
    """What a relationship between two entities is (relationship_service); its labels there, from either side."""

    SUPPLIES = "supplies"  # the subject supplies the object: supplier and customer
    COMPETES = "competes"  # symmetric: the subject is the one with the lower id
    REGULATES = "regulates"  # the subject, an authority or organisation, regulates the object
    DEPENDS_ON = "depends_on"  # the subject depends on the object, a commodity


class ReviewStatus(StrEnum):
    """The quality flag of a relationship: the analyst's review (the admin's decision of 2026-10-31)."""

    UNREVIEWED = "unreviewed"
    CONFIRMED = "confirmed"
    DISPUTED = "disputed"  # kept as history, not in effect


class RecordOrigin(StrEnum):
    """Who made an Angabe about a relationship."""

    REPORT = "report"
    ANALYST = "analyst"


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


class Event(Base):
    """One real-world event (UI term: Ereignis): an incident with its continuation, collecting every report of it. Its
    id is the stable event ID (datamodel.md, rule 1)."""

    __tablename__ = "event"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Of the report that founded it, in English: kept, so the event reads the same as reports come in.
    title: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(String(40))
    country: Mapped[str | None] = mapped_column(String(2))
    # The publication (else fetch) times of its first and latest report; the latest decides whether a new report may
    # still join it (event_service.OPEN_FOR).
    first_reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The report it is named after - title, type and country are this report's - which a split never takes from it.
    # use_alter: event and event_candidate refer to each other.
    founding_candidate_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("event_candidate.id", ondelete="SET NULL", use_alter=True), index=True
    )
    # Set by a merge, never cleared: the event its reports went to. Its id stays valid and leads there.
    merged_into_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("event.id", ondelete="SET NULL"))

    __table_args__ = (CheckConstraint(event_type.in_([s.value for s in EventType]), name="event_type"),)


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
    # Its event (event_service); empty until assigned.
    event_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("event.id", ondelete="SET NULL"), index=True)
    assignment_method: Mapped[str | None] = mapped_column(String(10))
    # Its similarity to the event when assigned (TF-IDF cosine); empty for one that founded its event.
    assignment_score: Mapped[float | None] = mapped_column(Float)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(event_type.in_([s.value for s in EventType]), name="event_type"),
        CheckConstraint(status.in_([s.value for s in EventStatus]), name="status"),
        CheckConstraint(assignment_method.in_([s.value for s in AssignmentMethod]), name="assignment_method"),
        CheckConstraint(inference_confidence.in_([s.value for s in ConfidenceLevel]), name="inference_confidence"),
    )


class EventState(Base):
    """One version of an event's state (UI term: Ereigniszustand), stated by one report (event_service.state_effect).
    Appended, never changed (datamodel.md, rule 3)."""

    __tablename__ = "event_state"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("event.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20))
    # The time of the report that stated it: its article's publication, else its fetch.
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The report; the version stays should its article ever go.
    candidate_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("event_candidate.id", ondelete="SET NULL"), index=True
    )
    # When Seismo learned it (datamodel.md, rule 11): a late report may state an earlier version.
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The correction that appended it; empty when its report's assignment did.
    added_by_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("event_correction.id"))
    # The correction it no longer follows from; set once, never cleared. A withdrawn version stays to be seen and is
    # never in effect.
    withdrawn_by_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("event_correction.id"))

    __table_args__ = (
        CheckConstraint(status.in_([s.value for s in EventStatus if s != EventStatus.UNKNOWN]), name="status"),
        Index("ix_event_state_event_id_valid_from", event_id, valid_from),
    )


class EventCorrection(Base):
    """A merge or split of events by the analyst (UI term: Korrektur), the audit trail of datamodel.md, rule 3. Never
    changed, never deleted."""

    __tablename__ = "event_correction"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    # The event the reports went to: of a merge the one that remains, of a split the new one.
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("event.id", ondelete="CASCADE"), index=True)
    # The events the reports left, each with the reports it gave, ids only: [{"event_id", "candidate_ids"}] - of a
    # merge every event merged, of a split the event split.
    moves: Mapped[list] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (CheckConstraint(kind.in_([s.value for s in CorrectionKind]), name="kind"),)


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


class Entity(Base):
    """A company, subsidiary, person, country, authority, organisation, product, commodity or sector (UI term:
    Entität), recognised in reports (entity_service). Its name forms and identifiers are its aliases."""

    __tablename__ = "entity"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    # The usual English name it was first recognised with, the German one of a starter list item. [privacy] for persons.
    name: Mapped[str] = mapped_column(Text)
    # Of a subsidiary the company that owns it, of a product the company that makes it.
    parent_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("entity.id", ondelete="SET NULL"), index=True)
    # Set by a merge, never cleared: the entity its name forms went to. Its id stays valid and leads there.
    merged_into_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("entity.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (CheckConstraint(kind.in_([s.value for s in EntityKind]), name="kind"),)


class EntityAlias(Base):
    """A name form or identifier of an entity (UI term: Namensform). Uncertain while the LLM only judged a similar
    name the same and the analyst has not confirmed it (datamodel.md, rule 9)."""

    __tablename__ = "entity_alias"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    entity_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("entity.id", ondelete="CASCADE"), index=True)
    scheme: Mapped[str] = mapped_column(String(20))
    value: Mapped[str] = mapped_column(Text)  # as recognised; [privacy] for persons
    # What it is looked up by (entity_service.name_key, identifier_key).
    key: Mapped[str] = mapped_column(Text)
    match: Mapped[str] = mapped_column(String(20))
    # Of an uncertain one: its trigram similarity to the entity's most similar name form when it came.
    similarity: Mapped[float | None] = mapped_column(Float)
    uncertain: Mapped[bool] = mapped_column(server_default=false())
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(scheme.in_([s.value for s in AliasScheme]), name="scheme"),
        CheckConstraint(match.in_([s.value for s in AliasMatch]), name="match"),
        Index("ix_entity_alias_scheme_key", scheme, key),
        # An identifier names one entity only.
        Index("uq_entity_alias_identifier", scheme, key, unique=True, postgresql_where=scheme != AliasScheme.NAME.value),
        # Similar names (pg_trgm): the candidates the LLM is shown.
        Index(
            "ix_entity_alias_key_trgm", key, postgresql_using="gin", postgresql_ops={"key": "gin_trgm_ops"},
            postgresql_where=scheme == AliasScheme.NAME.value,
        ),
    )


class EntityMention(Base):
    """An entity named in a report (UI term: Nennung): an actor or an impact's target of an event candidate, by the
    name form it was recognised with - which carries the uncertainty."""

    __tablename__ = "entity_mention"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    candidate_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("event_candidate.id", ondelete="CASCADE"), index=True)
    alias_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("entity_alias.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(10))
    position: Mapped[int] = mapped_column(SmallInteger)  # in the candidate's actors or impacts

    __table_args__ = (
        CheckConstraint(source.in_([s.value for s in MentionSource]), name="source"),
        UniqueConstraint(candidate_id, source, position),
    )


class EntityRecognition(Base):
    """The recognition of the entities an extraction's reports name (UI term: Entitätenerkennung): one LLM run per
    extraction, or none when they name nobody."""

    __tablename__ = "entity_recognition"

    extraction_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("extraction.id", ondelete="CASCADE"), primary_key=True)
    model: Mapped[str | None] = mapped_column(String(200))  # empty without a call
    schema_version: Mapped[str] = mapped_column(String(20))
    recognised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    names: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # the distinct names the model was asked about
    linked: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # of them, those linked to an entity
    # Angaben of relationships it recorded (schema 2).
    relationships: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class EntityRelationship(Base):
    """A relationship between two entities (UI term: Beziehung): who supplies, competes with, regulates or depends on
    whom. Its state - validity and review - follows from its Angaben (relationship_service.replay), never stored
    apart from them (datamodel.md, rules 3 and 10)."""

    __tablename__ = "entity_relationship"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    subject_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("entity.id", ondelete="CASCADE"))
    object_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("entity.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(kind.in_([s.value for s in RelationshipKind]), name="kind"),
        CheckConstraint(subject_id != object_id, name="two_entities"),
        UniqueConstraint(kind, subject_id, object_id),
    )


class RelationshipRecord(Base):
    """One Angabe about a relationship: a report states that it holds or ended, backed by the quote of one of its
    statements; or the analyst sets its validity and review, naming a source. Appended, never changed - only moved
    with the name form or entity a correction moves."""

    __tablename__ = "relationship_record"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    relationship_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("entity_relationship.id", ondelete="CASCADE"), index=True)
    origin: Mapped[str] = mapped_column(String(10))
    # Of a report's: the report, the statement whose Beleg backs it, and the name forms it names the two by. The
    # Angabe stays should its article go.
    candidate_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("event_candidate.id", ondelete="SET NULL"), index=True)
    statement_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("statement.id", ondelete="SET NULL"))
    subject_alias_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("entity_alias.id", ondelete="SET NULL"), index=True)
    object_alias_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("entity_alias.id", ondelete="SET NULL"), index=True)
    reported_on: Mapped[date | None] = mapped_column(Date)  # the report's day: its publication, else its fetch, in UTC
    ended: Mapped[bool] = mapped_column(server_default=false())  # the report says it ended
    # Of the analyst's: the validity and review it sets, and its source - a link or a note.
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    review: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(origin.in_([s.value for s in RecordOrigin]), name="origin"),
        CheckConstraint(review.in_([s.value for s in ReviewStatus]), name="review"),
    )


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
