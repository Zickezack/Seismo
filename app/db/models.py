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
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    literal_column,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


SEARCH_CONFIG = literal_column("'english'::regconfig")


def search_vector(words):
    """The English full-text vector of `words` (Dashboard, Recherche und Alerts, story 4): every text the model writes is
    English. Index and query state the same expression, else PostgreSQL does not use the index - a bound parameter in it
    would be another expression, so the configuration and the space are literals."""
    return func.to_tsvector(SEARCH_CONFIG, words)


def report_words(title, summary):
    """What the search reads of a report: its title and summary."""
    return title.op("||")(literal_column("' '")).op("||")(summary)


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
    REPROCESS = "reprocess"  # reports of an event replaced by their article's new extraction (reprocessing_service)


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


class ReprocessingStep(StrEnum):
    """What a reprocessing runs again (reprocessing_service)."""

    EXTRACTION = "extraction"  # the event extraction; its reports' assignment and recognition follow
    RECOGNITION = "recognition"  # the entity recognition of an extraction


class ReprocessingStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"  # every run it selected was queued once
    CANCELLED = "cancelled"  # by the admin; jobs queued before still run


class InstrumentKind(StrEnum):
    STOCK = "stock"
    ETF = "etf"
    INDEX = "index"  # in points, without a currency


class PriceResolution(StrEnum):
    """What a Kurspunkt covers: one price at its time, or an interval."""

    QUOTE = "quote"  # the last price at its time, with the day's open, high, low and volume so far
    MINUTE = "minute"  # a minute's open, high, low, close and volume, `at` its start (a tracked one's, Alpaca)
    HOUR = "hour"  # the minutes or quotes of an hour once their full resolution was kept long enough, `at` its start
    DAY = "day"  # a trading day: open, high, low, close and volume, `at` its start as the source gives it


class GapCause(StrEnum):
    """Why a tracked Wertpapier's prices were not collected for a while (UI term: Erfassungslücke)."""

    NOT_ASKED = "not_asked"  # no collection ran: the worker stopped, the queue stuck
    SOURCE_ERROR = "source_error"  # the source failed or sent no price for it


class PriceSource(StrEnum):
    """Where a Wertpapier's prices come from (app/prices/)."""

    ALPACA = "alpaca"  # US stocks and ETFs: live prices and the daily history
    SIX = "six"  # SIX Swiss Exchange: live prices
    EODHD = "eodhd"  # worldwide, with a paid plan: live prices


class InstrumentStatus(StrEnum):
    ACTIVE = "active"  # traded
    INACTIVE = "inactive"  # no longer traded: delisted, merged away


class WatchItemKind(StrEnum):
    """What a watchlist item follows (UI term: Watchlist): an event matches when it, or one of its reports, has it."""

    ENTITY = "entity"  # a report names it
    EVENT_TYPE = "event_type"
    COUNTRY = "country"
    EVENT = "event"  # the event itself
    INSTRUMENT = "instrument"  # a report names the Entität it is linked to
    SITUATION = "situation"  # it belongs to the Lage


class AlertTrigger(StrEnum):
    """What a hit of an alert rule is (alert_service)."""

    NEW = "new"  # the event matches for the first time, first reported while the rule is active
    CHANGE = "change"  # a change of its state: an escalation, a closure, a resumption
    REPORT = "report"  # a new report of it


class SituationStatus(StrEnum):
    """Where a Lage stands (situation_service)."""

    PROPOSED = "proposed"  # proposed by the LLM, waiting for the analyst
    CONFIRMED = "confirmed"  # in effect
    REJECTED = "rejected"  # by the analyst
    DISMISSED = "dismissed"  # the LLM judged the events no Lage: kept, so it is not asked again
    MERGED = "merged"  # into another one by the analyst


class MemberStatus(StrEnum):
    """Where an event's belonging to a Lage stands."""

    PROPOSED = "proposed"  # with a proposed Lage, or to a dormant one: not in effect until the analyst confirms it
    AUTOMATIC = "automatic"  # assigned by the system: in effect, flagged until the analyst confirms it
    CONFIRMED = "confirmed"  # by the analyst
    REMOVED = "removed"  # by the analyst, or moved by a merge or split: never assigned again by the system


class MemberOrigin(StrEnum):
    PROPOSAL = "proposal"  # one of the events the LLM proposed a Lage with
    SCORE = "score"  # similar enough by its words and entities
    LLM = "llm"  # in between, the LLM judged it part of the Lage
    ANALYST = "analyst"  # added, split off or merged by the analyst


class ImpactPath(StrEnum):
    """How an event reaches the target of a Wirkungsannahme (impact_service)."""

    DIRECT = "direct"  # the event itself affects it
    INDIRECT = "indirect"  # through a relationship of an entity the event affects


class ImpactStatus(StrEnum):
    """A Wirkungsannahme's state, per version (impact_service)."""

    PROPOSED = "proposed"  # by the LLM, not reviewed yet
    CONFIRMED = "confirmed"  # by the analyst
    REJECTED = "rejected"  # by the analyst, with a reason
    SUPERSEDED = "superseded"  # proposed, then no longer proposed by a later assessment


class ImpactClass(StrEnum):
    """How well a Wirkungsannahme is founded, by the rule of impact_service.classify - never above what its evidence
    allows."""

    EVIDENCED = "evidenced"  # a report names the effect itself, with its quote
    PLAUSIBLE = "plausible"  # follows through a relationship, a source's forecast or a named effect
    SPECULATIVE = "speculative"  # neither


class ImpactOrigin(StrEnum):
    LLM = "llm"
    ANALYST = "analyst"


# The directions and horizons of an effect, as the event schema has them (app/llm/event_schema.py).
IMPACT_DIRECTIONS = ("positive", "negative", "mixed", "unclear")
IMPACT_HORIZONS = ("days", "weeks", "months", "years", "unknown")


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
    # The reprocessing's budget (reprocessing_service), apart from the extraction's.
    reprocessings_per_hour: Mapped[int] = mapped_column(server_default="30")
    # Live prices (price_service) and the daily history (history_service).
    price_interval_minutes: Mapped[int] = mapped_column(server_default="30")
    price_history_years: Mapped[int] = mapped_column(server_default="2")
    # Days a tracked Wertpapier's prices keep their full resolution before they are compressed to hours; 0: always.
    price_full_resolution_days: Mapped[int] = mapped_column(server_default="90")
    tracked_instruments_max: Mapped[int] = mapped_column(server_default="50")
    # Days of Alpaca's minutes before tracking began that are fetched afterwards; 0: none.
    minute_backfill_days: Mapped[int] = mapped_column(server_default="90")
    # The Lagen (situation_service): open this long after their latest event; LLM calls per hour for them.
    situation_open_days: Mapped[int] = mapped_column(server_default="30")
    situation_llm_calls_per_hour: Mapped[int] = mapped_column(server_default="20")
    # The assessment of an event's effects (impact_service): LLM calls per hour for it; 0 holds it.
    impact_llm_calls_per_hour: Mapped[int] = mapped_column(server_default="20")
    # The event study's periods (event_study_service): an event of its first report's day up to the first is in
    # training, up to the second in validation, after the last one set in test; neither set: not divided.
    study_training_until: Mapped[date | None] = mapped_column(Date)
    study_validation_until: Mapped[date | None] = mapped_column(Date)
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
        # 0 holds the reprocessing.
        CheckConstraint(reprocessings_per_hour >= 0, name="reprocessings_per_hour"),
        CheckConstraint(price_interval_minutes.between(5, 1440), name="price_interval_minutes"),
        CheckConstraint(price_history_years.between(1, 30), name="price_history_years"),
        CheckConstraint(price_full_resolution_days.between(0, 3650), name="price_full_resolution_days"),
        CheckConstraint(tracked_instruments_max.between(0, 1000), name="tracked_instruments_max"),
        CheckConstraint(minute_backfill_days.between(0, 3650), name="minute_backfill_days"),
        CheckConstraint(situation_open_days.between(1, 365), name="situation_open_days"),
        # 0 holds the forming of Lagen.
        CheckConstraint(situation_llm_calls_per_hour >= 0, name="situation_llm_calls_per_hour"),
        CheckConstraint(impact_llm_calls_per_hour.between(0, 1000), name="impact_llm_calls_per_hour"),
        # Validation only after a training period, and after its end.
        CheckConstraint(
            "study_validation_until IS NULL OR (study_training_until IS NOT NULL AND study_validation_until > study_training_until)",
            name="study_periods",
        ),
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


class Reprocessing(Base):
    """The runs of one step made with an older schema version or another model, run again for the articles chosen
    (UI term: Neuverarbeitung): behind live work, within its hourly budget (reprocessing_service)."""

    __tablename__ = "reprocessing"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    step: Mapped[str] = mapped_column(String(20))
    # The runs it replaces: made with this schema version and this model.
    schema_version: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(200))
    # Only the articles of this Quelle, and a copy of its name. A Quelle deleted since leaves the id empty, and its
    # articles, without theirs, match no more.
    source_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("source.id", ondelete="SET NULL"))
    source_name: Mapped[str | None] = mapped_column(String(200))
    # Only the articles of these days (publication, else fetch, UTC); empty: no limit.
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), server_default=ReprocessingStatus.RUNNING.value)
    total: Mapped[int]  # the runs it selected when it started
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(step.in_([s.value for s in ReprocessingStep]), name="step"),
        CheckConstraint(status.in_([s.value for s in ReprocessingStatus]), name="status"),
        CheckConstraint(period_start <= period_end, name="period"),
    )


class StorageMeasurement(Base):
    """What the data took on one day (UI term: Speichermessung), measured by the worker once a day (storage_service)."""

    __tablename__ = "storage_measurement"

    measured_on: Mapped[date] = mapped_column(Date, primary_key=True)  # the UTC day
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    database_bytes: Mapped[int] = mapped_column(BigInteger)
    # Every table's size with its indexes and TOAST: {"name": bytes}.
    table_bytes: Mapped[dict] = mapped_column(JSONB)
    article_count: Mapped[int] = mapped_column(BigInteger)
    raw_text_count: Mapped[int] = mapped_column(BigInteger)
    raw_text_bytes: Mapped[int] = mapped_column(BigInteger)  # as stored, compressed: the answers and their plain texts
    backup_count: Mapped[int]
    backup_bytes: Mapped[int] = mapped_column(BigInteger)
    # Free on the backup store's file system; empty when it could not be read.
    backup_free_bytes: Mapped[int | None] = mapped_column(BigInteger)


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
    # When its raw text was deleted, its source's retention period over (raw_text_service); the metadata above stays.
    raw_text_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
    # The extraction of its article that replaced it (reprocessing_service): its reports leave their events once the
    # new ones have theirs. Empty for the article's current reading.
    replaced_by_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("extraction.id", ondelete="SET NULL"), index=True)
    # The Neuverarbeitung that made it; empty for the live extraction and an article's own reprocessing.
    reprocessing_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("reprocessing.id", ondelete="SET NULL"), index=True)

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
        # The historical search (event_repository.EventFilter.text).
        Index("ix_event_candidate_search", search_vector(report_words(title, summary)), postgresql_using="gin"),
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
    """A merge or split of events by the analyst, or the reports a reprocessing replaced leaving their event (UI term:
    Korrektur), the audit trail of datamodel.md, rule 3. Never changed, never deleted."""

    __tablename__ = "event_correction"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    # The event the reports went to: of a merge the one that remains, of a split the new one; of a reprocessing the
    # one they left, for none.
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("event.id", ondelete="CASCADE"), index=True)
    # The events the reports left, each with the reports it gave, ids only: [{"event_id", "candidate_ids"}] - of a
    # merge every event merged, of a split the event split, of a reprocessing the event itself.
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
        Index("ix_statement_search", search_vector(text), postgresql_using="gin"),
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
    # The Neuverarbeitung of this run. A run again replaces the row, the mentions and the Angaben of the one before.
    reprocessing_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("reprocessing.id", ondelete="SET NULL"), index=True)


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
    # Of a report's: when the reading it came from was replaced - its article extracted again, or its extraction's
    # entities recognised again (reprocessing_service). From then on it no longer counts; it stays in the history.
    replaced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(origin.in_([s.value for s in RecordOrigin]), name="origin"),
        CheckConstraint(review.in_([s.value for s in ReviewStatus]), name="review"),
    )


class Instrument(Base):
    """A tradable security (UI term: Wertpapier): a stock, an ETF or an index, by its symbol at an exchange of its price
    source (price_service)."""

    __tablename__ = "instrument"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source: Mapped[str] = mapped_column(String(20))
    # Its id at the source, stable where the source keeps one through a change of symbol: Alpaca's asset id, SIX's
    # ValorId (ISIN, currency, market), EODHD's code ("NESN.SW").
    source_ref: Mapped[str] = mapped_column(String(100))
    # As the source names it now: the symbol, and the exchange (Alpaca: NASDAQ, NYSE, ...; EODHD's code).
    symbol: Mapped[str] = mapped_column(String(30))
    exchange: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10))
    currency: Mapped[str | None] = mapped_column(String(3))  # ISO 4217; empty for an index, in points
    isin: Mapped[str | None] = mapped_column(String(12))
    cusip: Mapped[str | None] = mapped_column(String(9))
    # The Entität it belongs to: by its ISIN or ticker among the name forms, by its name - then uncertain -, or chosen.
    entity_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("entity.id", ondelete="SET NULL"), index=True)
    entity_uncertain: Mapped[bool] = mapped_column(server_default=false())
    # Its latest price is fetched at the interval of the Plattform-Einstellungen (UI: Live-Kurs).
    live: Mapped[bool] = mapped_column(server_default=true())
    # Its prices are collected continuously at the highest resolution its source offers (tracking_service, UI: verfolgt).
    tracked: Mapped[bool] = mapped_column(server_default=false())
    # When it was last switched on; kept when switched off, so its full-resolution points are still compressed.
    tracked_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Its prices are collected up to here: Alpaca's minutes up to this time, of the others the last collection.
    collected_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Of Alpaca's: its minutes before tracking began are fetched afterwards up to here, until it reaches tracked_since.
    backfill_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(10), server_default=InstrumentStatus.ACTIVE.value)
    delisted_on: Mapped[date | None] = mapped_column(Date)
    # Its daily prices are fetched (history_service), each trading day up to and including history_through.
    history: Mapped[bool] = mapped_column(server_default=false())
    history_through: Mapped[date | None] = mapped_column(Date)
    # The fetch's outcome for it: shown to the admin, never logged (datamodel.md, rule 13).
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(kind.in_([s.value for s in InstrumentKind]), name="kind"),
        CheckConstraint(source.in_([s.value for s in PriceSource]), name="source"),
        CheckConstraint(status.in_([s.value for s in InstrumentStatus]), name="status"),
        UniqueConstraint(source, source_ref),
        Index("ix_instrument_live", live),
        Index("ix_instrument_tracked", tracked),
        Index("ix_instrument_symbol", source, symbol),
    )


class PricePoint(Base):
    """One price of a Wertpapier (UI term: Kurspunkt), as received - unadjusted, in UTC, with the time it became
    available (datamodel.md, rule 14)."""

    __tablename__ = "price_point"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instrument.id", ondelete="CASCADE"))
    resolution: Mapped[str] = mapped_column(String(10))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # the price's own time, as the source states it
    open: Mapped[float | None] = mapped_column(Numeric(20, 6))
    high: Mapped[float | None] = mapped_column(Numeric(20, 6))
    low: Mapped[float | None] = mapped_column(Numeric(20, 6))
    close: Mapped[float] = mapped_column(Numeric(20, 6))  # of a quote: its price
    volume: Mapped[int | None] = mapped_column(BigInteger)
    previous_close: Mapped[float | None] = mapped_column(Numeric(20, 6))  # of a quote: the last trading day's close
    source: Mapped[str] = mapped_column(String(20))
    delayed: Mapped[bool]  # the source delays it (EODHD: 15 to 20 minutes); else real time
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Of an hour: the points it replaced (tracking_service.compress); empty for a price as received.
    samples: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint(resolution.in_([s.value for s in PriceResolution]), name="resolution"),
        # A price asked for again unchanged - the market closed - is stored once.
        UniqueConstraint(instrument_id, resolution, at),
    )


class PriceGap(Base):
    """A time a tracked Wertpapier's prices were not collected (UI term: Erfassungslücke): recorded, never filled
    (requirements.md, Live-Kurse und Kursdatenspeicher, criterion 2)."""

    __tablename__ = "price_gap"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instrument.id", ondelete="CASCADE"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # the last collection before it
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # the first after it
    cause: Mapped[str] = mapped_column(String(20))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(cause.in_([s.value for s in GapCause]), name="cause"),
        CheckConstraint(ends_at > starts_at, name="period"),
    )


class CorporateAction(Base):
    """A split, dividend, change of symbol, delisting or merger of a Wertpapier (UI term: Kapitalmassnahme), as its
    source sends it: kept apart from the unadjusted prices (datamodel.md, rule 14)."""

    __tablename__ = "corporate_action"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instrument.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(20))
    source_ref: Mapped[str] = mapped_column(String(100))  # its id at the source; a merger's names both Wertpapiere
    kind: Mapped[str] = mapped_column(String(30))  # as the source names it: forward_split, cash_dividend, name_change, ...
    ex_date: Mapped[date | None] = mapped_column(Date)
    effective_date: Mapped[date | None] = mapped_column(Date)  # of a merger
    process_date: Mapped[date | None] = mapped_column(Date)  # when the source processed it: when it was known
    old_rate: Mapped[float | None] = mapped_column(Numeric(24, 12))
    new_rate: Mapped[float | None] = mapped_column(Numeric(24, 12))
    rate: Mapped[float | None] = mapped_column(Numeric(24, 12))  # a dividend per share, a merger's ratio
    old_symbol: Mapped[str | None] = mapped_column(String(30))
    new_symbol: Mapped[str | None] = mapped_column(String(30))
    currency: Mapped[str | None] = mapped_column(String(3))
    details: Mapped[dict] = mapped_column(JSONB)  # the record as sent
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint(source, source_ref, instrument_id),)


class TradingDay(Base):
    """A trading day of the US exchanges (UI term: Handelstag), from the price source's calendar: what a gap is, and when
    a day's prices are final."""

    __tablename__ = "trading_day"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MarketDataSync(Base):
    """How far a sync of market data came (history_service): the list of Wertpapiere, the calendar, the corporate
    actions. Not shown as such."""

    __tablename__ = "market_data_sync"

    name: Mapped[str] = mapped_column(String(30), primary_key=True)
    through: Mapped[date | None] = mapped_column(Date)  # every day up to and including this one
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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


class Watchlist(Base):
    """What an analyst follows (UI term: Watchlist): entities, event types, countries, events and Wertpapiere."""

    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("uq_watchlist_name", func.lower(name), unique=True),)


class WatchlistItem(Base):
    """One thing a watchlist follows, by its kind: an entity, an event or a Wertpapier by id, a type or a country by
    its code."""

    __tablename__ = "watchlist_item"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    watchlist_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("watchlist.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))
    # As chosen, a merged one too: the match follows it to the entity or event it went into.
    entity_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("entity.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("event.id", ondelete="CASCADE"), index=True)
    instrument_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("instrument.id", ondelete="CASCADE"), index=True)
    # As chosen, a merged one too: the match follows it to the Lage it went into.
    situation_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("situation.id", ondelete="CASCADE"), index=True)
    value: Mapped[str | None] = mapped_column(String(40))  # an event type, or an ISO 3166-1 alpha-2 code
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(kind.in_([s.value for s in WatchItemKind]), name="kind"),
        CheckConstraint(
            "num_nonnulls(entity_id, event_id, instrument_id, situation_id, value) = 1 AND CASE kind"
            " WHEN 'entity' THEN entity_id IS NOT NULL WHEN 'event' THEN event_id IS NOT NULL"
            " WHEN 'instrument' THEN instrument_id IS NOT NULL WHEN 'situation' THEN situation_id IS NOT NULL"
            " ELSE value IS NOT NULL END",
            name="target",
        ),
        # Each thing once per watchlist.
        Index(
            "uq_watchlist_item_target", watchlist_id, kind,
            func.coalesce(value, func.cast(func.coalesce(entity_id, event_id, instrument_id, situation_id), Text)), unique=True,
        ),
    )


class AlertRule(Base):
    """A rule that notifies about the events that match it (UI term: Alert-Regel): new ones, changes of their state,
    new reports - at most a number per hour, an event again only after a quiet period (alert_service)."""

    __tablename__ = "alert_rule"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(server_default=true())
    # The events it covers: those its watchlist follows, if it has one, and that match its filter.
    watchlist_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("watchlist.id"), index=True)
    # The filter as the event list's URL states it, without period (event_service.parse_filter).
    criteria: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    on_new: Mapped[bool] = mapped_column(server_default=true())
    on_change: Mapped[bool] = mapped_column(server_default=true())
    on_report: Mapped[bool] = mapped_column(server_default=false())
    max_per_hour: Mapped[int] = mapped_column(server_default="5")
    cooldown_hours: Mapped[int] = mapped_column(server_default="6")
    # What happens from then on counts: set when it is created, saved or activated again.
    active_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(max_per_hour.between(1, 100), name="max_per_hour"),
        CheckConstraint(cooldown_hours.between(0, 168), name="cooldown_hours"),
        CheckConstraint(on_new | on_change | on_report, name="trigger"),
        Index("uq_alert_rule_name", func.lower(name), unique=True),
    )


class Alert(Base):
    """A notification of an alert rule (UI term: Alert): one event's hits, or a Sammel-Alert of several once the
    rule's hourly limit is reached."""

    __tablename__ = "alert"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    rule_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("alert_rule.id", ondelete="CASCADE"), index=True)
    digest: Mapped[bool] = mapped_column(server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_alert_unread", created_at, postgresql_where=read_at.is_(None)),)


class AlertHit(Base):
    """What an alert rule found (UI term: Treffer), each once: an event new to it, a change of state, a report. Waits
    while the rule is throttled, then goes out with an Alert."""

    __tablename__ = "alert_hit"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    rule_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("alert_rule.id", ondelete="CASCADE"))
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("event.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(10))
    state_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("event_state.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("event_candidate.id", ondelete="CASCADE"), index=True
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The Alert it went out with; empty while it waits.
    alert_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("alert.id", ondelete="CASCADE"), index=True)

    __table_args__ = (
        CheckConstraint(trigger.in_([s.value for s in AlertTrigger]), name="trigger"),
        CheckConstraint(
            "CASE trigger WHEN 'change' THEN state_id IS NOT NULL WHEN 'report' THEN candidate_id IS NOT NULL"
            " ELSE true END",
            name="target",
        ),
        Index("ix_alert_hit_rule_id_event_id", rule_id, event_id),
        Index("ix_alert_hit_waiting", rule_id, postgresql_where=alert_id.is_(None)),
        # Each found once.
        Index("uq_alert_hit_new", rule_id, event_id, unique=True, postgresql_where=trigger == AlertTrigger.NEW.value),
        Index("uq_alert_hit_change", rule_id, state_id, unique=True, postgresql_where=trigger == AlertTrigger.CHANGE.value),
        Index(
            "uq_alert_hit_report", rule_id, candidate_id, unique=True, postgresql_where=trigger == AlertTrigger.REPORT.value
        ),
    )


class Situation(Base):
    """Related events over weeks (UI term: Lage) - a war, a trade conflict, a series of attacks -: a level above the
    Ereignis, which stays one incident. Proposed by the LLM, in effect once the analyst confirms it (situation_service)."""

    __tablename__ = "situation"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # In English, like the events' titles; the analyst may rename it.
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), server_default=SituationStatus.PROPOSED.value)
    # Set by a merge, never cleared: the Lage its events went to.
    merged_into_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("situation.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When the analyst confirmed, rejected or merged it, or the LLM dismissed it.
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(status.in_([s.value for s in SituationStatus]), name="status"),
        Index("ix_situation_status", status),
    )


class SituationMember(Base):
    """An event's belonging to a Lage (UI term: Lage-Zuordnung); an event may belong to several. One row per pair:
    a removed one stays, so the system never assigns the event to that Lage again."""

    __tablename__ = "situation_member"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    situation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("situation.id", ondelete="CASCADE"))
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("event.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20))
    origin: Mapped[str] = mapped_column(String(20))
    # Its similarity to the Lage when assigned, 0 to 1; empty for one the analyst added.
    score: Mapped[float | None] = mapped_column(Float)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When the analyst confirmed or removed it, or a merge or split moved it.
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Of one moved by a merge or split: the Lage it went to.
    moved_to_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("situation.id", ondelete="SET NULL"))

    __table_args__ = (
        CheckConstraint(status.in_([s.value for s in MemberStatus]), name="status"),
        CheckConstraint(origin.in_([s.value for s in MemberOrigin]), name="origin"),
        UniqueConstraint(situation_id, event_id),
    )


class SituationCheck(Base):
    """When an event was last weighed against the Lagen (situation_service): again once it has a new report or a Lage
    was confirmed since. Not shown in the UI."""

    __tablename__ = "situation_check"

    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("event.id", ondelete="CASCADE"), primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImpactAssessment(Base):
    """One LLM run weighing an event's effects (UI term: Wirkungsbewertung; impact_service): what it was asked with,
    so every proposed version can be traced to it (datamodel.md, rule 2)."""

    __tablename__ = "impact_assessment"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("event.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[int] = mapped_column(SmallInteger)
    # The event's reports when it was asked: a new one makes it due again.
    reports: Mapped[int] = mapped_column(Integer)
    # How many hypotheses the answer held after the checks, and how many the checks left out.
    hypotheses: Mapped[int] = mapped_column(Integer)
    dropped: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImpactHypothesis(Base):
    """An assumed effect of an event on an entity - a company, a sector, a commodity - directly or through one of its
    relationships (UI term: Wirkungsannahme). What it says lives in its versions, appended and never changed
    (datamodel.md, rule 3); its identity is the event, the target and the way."""

    __tablename__ = "impact_hypothesis"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("event.id", ondelete="CASCADE"), index=True)
    # Entities are never deleted, only merged: a page follows merged_into_id.
    target_entity_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("entity.id"), index=True)
    path: Mapped[str] = mapped_column(String(10))
    # Of an indirect one: the relationship, and the entity of the event at its other side.
    via_relationship_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("entity_relationship.id", ondelete="SET NULL"))
    via_entity_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("entity.id"))
    origin: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(path.in_([s.value for s in ImpactPath]), name="path"),
        CheckConstraint(origin.in_([s.value for s in ImpactOrigin]), name="origin"),
        CheckConstraint("(path = 'direct') = (via_entity_id IS NULL)", name="via"),
    )


class ImpactHypothesisVersion(Base):
    """One version of a Wirkungsannahme: a proposal of an assessment, or the analyst's review or change."""

    __tablename__ = "impact_hypothesis_version"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    hypothesis_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("impact_hypothesis.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12))
    direction: Mapped[str] = mapped_column(String(10))
    horizon: Mapped[str] = mapped_column(String(10))
    mechanism: Mapped[str] = mapped_column(Text)
    # Never empty in the UI: none known is said as such.
    counterarguments: Mapped[str] = mapped_column(Text)
    classification: Mapped[str] = mapped_column(String(12))
    # What the LLM proposed, before the rule capped it; None for the analyst's.
    proposed_classification: Mapped[str | None] = mapped_column(String(12))
    # The Belege it cites from the event's reports: [{"statement_id": 1} | {"candidate_id": 2, "impact": 0}].
    support: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    against: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    origin: Mapped[str] = mapped_column(String(10))
    assessment_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("impact_assessment.id", ondelete="SET NULL"))
    # The analyst's reason: required to reject.
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(hypothesis_id, number),
        CheckConstraint(status.in_([s.value for s in ImpactStatus]), name="status"),
        CheckConstraint(direction.in_(IMPACT_DIRECTIONS), name="direction"),
        CheckConstraint(horizon.in_(IMPACT_HORIZONS), name="horizon"),
        CheckConstraint(classification.in_([s.value for s in ImpactClass]), name="classification"),
        CheckConstraint(origin.in_([s.value for s in ImpactOrigin]), name="origin"),
    )
