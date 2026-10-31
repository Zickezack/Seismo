"""Reprocessing (userstories.md, Verdichtung und Daten-Lifecycle, story 2): the runs of a step made with an older
schema version or another model are run again for the articles the admin chooses, so the knowledge base takes up a
better schema or model without the whole history being processed again (architecture.md, principle 5).

A Neuverarbeitung selects the current runs of one step - the event extraction or the entity recognition - by their
schema version and model, optionally of one Quelle and a period. The worker queues their jobs behind every live job
(priority 1), at most a number per hour (Plattform-Einstellung), the newest article first; the reprocessing is done
once each was queued and none waits or runs. One runs at a time. An article's page reprocesses its own at once.

A new extraction is the article's new reading. Its reports are assigned as usual and find their events through the
old reading's reports, which leave theirs once the new ones have theirs (retire_replaced: a Korrektur of each event
they leave, event_correction_service.replace_reports); the entity recognition follows on its own. The old reading
stays, marked replaced, and so do the versions its reports stated, withdrawn. Its Angaben of relationships no longer
count from the moment it was replaced. A recognition run again replaces its extraction's mentions, and the Angaben of
the run before no longer count either (entity_service.recognise).
"""
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import Engine, Row

from app.db.models import BACKFILL_PRIORITY, LIVE_PRIORITY, ReprocessingStatus, ReprocessingStep
from app.llm import entity_schema, event_schema
from app.repositories import (
    entity_repository,
    event_repository,
    extraction_repository,
    reprocessing_repository,
    source_repository,
)
from app.repositories.reprocessing_repository import Selection
from app.services import event_correction_service, job_service, platform_settings_service, source_service
from app.services.bulk import BulkResult

logger = logging.getLogger(__name__)

BUDGET_PERIOD = timedelta(hours=1)
LIST_LIMIT = 20
# Replaced readings whose reports leave their events per check; the rest follow a minute later.
RETIRE_LIMIT = 100

STEP_LABELS = {ReprocessingStep.EXTRACTION: "Ereignisextraktion", ReprocessingStep.RECOGNITION: "Entitätenerkennung"}
STATUS_LABELS = {
    ReprocessingStatus.RUNNING: "Läuft",
    ReprocessingStatus.DONE: "Abgeschlossen",
    ReprocessingStatus.CANCELLED: "Abgebrochen",
}
JOB_KINDS = {
    ReprocessingStep.EXTRACTION: job_service.REEXTRACT_EVENTS_JOB_KIND,
    ReprocessingStep.RECOGNITION: job_service.RERECOGNISE_ENTITIES_JOB_KIND,
}
SCHEMA_VERSIONS = {ReprocessingStep.EXTRACTION: event_schema.SCHEMA_VERSION, ReprocessingStep.RECOGNITION: entity_schema.SCHEMA_VERSION}


class InvalidReprocessing(ValueError):
    """Invalid entries from the reprocessing form, or nothing to reprocess. The message is shown to the admin as is."""


@dataclass(frozen=True)
class Group:
    """The current runs of a step made with one schema version and one model (UI: Stand)."""

    step: str
    schema_version: str
    model: str | None
    count: int
    current: bool  # made as a run would be made now: nothing to reprocess

    @property
    def key(self) -> str:
        """The form's value: step, version and model."""
        return f"{self.step}|{self.schema_version}|{self.model or ''}"

    @property
    def step_label(self) -> str:
        return STEP_LABELS[self.step]


@dataclass(frozen=True)
class ReprocessingView:
    id: int
    step: str
    schema_version: str
    model: str
    source_name: str | None
    period_start: date | None
    period_end: date | None
    status: str
    total: int
    made: int  # the runs it made so far
    open_jobs: int  # waiting or running
    failed_jobs: int  # while the jobs are kept (Plattform-Einstellungen)
    created_at: datetime
    finished_at: datetime | None

    @property
    def step_label(self) -> str:
        return STEP_LABELS[self.step]

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def is_running(self) -> bool:
        return self.status == ReprocessingStatus.RUNNING

    @property
    def period_label(self) -> str:
        if not self.period_start and not self.period_end:
            return ""
        start = self.period_start.strftime("%d.%m.%Y") if self.period_start else "…"
        end = self.period_end.strftime("%d.%m.%Y") if self.period_end else "…"
        return f"{start} bis {end}"


@dataclass(frozen=True)
class Preview:
    """What a reprocessing with the form's choice would take."""

    group: Group
    source_name: str | None
    period_start: date | None
    period_end: date | None
    count: int
    without_text: int  # extractions it cannot take: their articles' texts are gone


@dataclass(frozen=True)
class Overview:
    groups: dict[str, list[Group]]  # by step
    reprocessings: list[ReprocessingView]  # the latest, newest first
    sources: list[source_service.Source]
    budget: int  # per hour
    preview: Preview | None = None
    model: str = ""  # the one a run would be made with now

    @property
    def running(self) -> ReprocessingView | None:
        return next((each for each in self.reprocessings if each.is_running), None)

    @property
    def outdated(self) -> list[Group]:
        return [group for groups in self.groups.values() for group in groups if not group.current]


@dataclass(frozen=True)
class ArticleState:
    """Whether an article's current runs are as a run would be made now; the article page offers the others."""

    extraction_id: int | None
    outdated: dict[str, str] = field(default_factory=dict)  # step: how its run was made

    @property
    def extraction_outdated(self) -> bool:
        return ReprocessingStep.EXTRACTION in self.outdated

    @property
    def recognition_outdated(self) -> bool:
        return ReprocessingStep.RECOGNITION in self.outdated


def groups(engine: Engine, model: str) -> dict[str, list[Group]]:
    """The current runs of each step by schema version and model; `model` the one a run would be made with now."""
    with engine.connect() as connection:
        return {step: _groups(connection, step, model) for step in ReprocessingStep}


def _groups(connection, step: str, model: str) -> list[Group]:
    return [
        Group(step, row.schema_version, row.model, row.count, row.schema_version == SCHEMA_VERSIONS[step] and row.model == model)
        for row in reprocessing_repository.groups(connection, step)
    ]


def overview(engine: Engine, model: str, form: Mapping[str, str] | None = None) -> Overview:
    """The page's content; with a choice in `form` (the step's group, a Quelle, a period) what it would take."""
    preview = None
    with engine.connect() as connection:
        by_step = {step: _groups(connection, step, model) for step in ReprocessingStep}
        settings = platform_settings_service.load(connection)
        if form and form.get("group"):
            group, selection, source_name = _parse(connection, form, by_step)
            count, without_text = reprocessing_repository.count(connection, selection)
            preview = Preview(group, source_name, selection.period_start, selection.period_end, count, without_text)
        rows = reprocessing_repository.latest(connection, LIST_LIMIT, JOB_KINDS)
    return Overview(
        by_step, [_view(row) for row in rows], source_service.list_sources(engine), settings.reprocessings_per_hour, preview, model
    )


def start(engine: Engine, form: Mapping[str, str], model: str) -> ReprocessingView:
    """Start a reprocessing of the chosen runs; its jobs wait behind every live job. One at a time."""
    with engine.begin() as connection:
        by_step = {step: _groups(connection, step, model) for step in ReprocessingStep}
        group, selection, source_name = _parse(connection, form, by_step)
        if reprocessing_repository.running(connection) is not None:
            raise InvalidReprocessing("Es läuft bereits eine Neuverarbeitung; abwarten oder abbrechen.")
        total, _without_text = reprocessing_repository.count(connection, selection)
        if not total:
            raise InvalidReprocessing("Die Auswahl umfasst nichts, was neu verarbeitet werden kann.")
        values = {
            "step": selection.step, "schema_version": selection.schema_version, "model": selection.model,
            "source_id": selection.source_id, "source_name": source_name, "period_start": selection.period_start,
            "period_end": selection.period_end, "total": total,
        }
        reprocessing_id = reprocessing_repository.insert_reprocessing(connection, values)
        rows = reprocessing_repository.latest(connection, LIST_LIMIT, JOB_KINDS)
    logger.info("Reprocessing started", extra={"reprocessing_id": reprocessing_id, "step": group.step, "total": total})
    return next(_view(row) for row in rows if row.id == reprocessing_id)


def cancel(engine: Engine, reprocessing_ids: Iterable[int]) -> BulkResult:
    """Stop running reprocessings, all or nothing; their jobs queued before still run.

    Atomic (requirements.md, "Destructive and Bulk Actions"): one that is not running blocks all of them.
    """
    reprocessing_ids = sorted(set(reprocessing_ids))
    if not reprocessing_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        found = {row.id: row for row in reprocessing_repository.lock_reprocessings(connection, reprocessing_ids)}
        blocked = []
        for reprocessing_id in reprocessing_ids:
            row = found.get(reprocessing_id)
            if row is None:
                blocked.append(f"Neuverarbeitung #{reprocessing_id} (nicht gefunden)")
            elif row.status != ReprocessingStatus.RUNNING:
                blocked.append(f"Neuverarbeitung #{reprocessing_id} ({STATUS_LABELS[row.status]})")
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        reprocessing_repository.finish(connection, reprocessing_ids, ReprocessingStatus.CANCELLED)
    logger.info("Reprocessing cancelled", extra={"reprocessing_ids": reprocessing_ids})
    return BulkResult(processed=len(reprocessing_ids), blocked=[])


def enqueue_due(engine: Engine) -> list[int]:
    """Queue the next jobs of the running reprocessing, as many as the hour's budget leaves, the newest article first;
    end it once every run it selects was queued and none of its jobs waits or runs. The worker calls it only while
    Ollama serves the model."""
    with engine.begin() as connection:
        run = reprocessing_repository.running(connection)
        if run is None:
            return []
        kind = JOB_KINDS[run.step]
        settings = platform_settings_service.load(connection)
        left = settings.reprocessings_per_hour - reprocessing_repository.jobs_since(connection, JOB_KINDS.values(), BUDGET_PERIOD)
        # At least one, to tell whether anything is left while the budget is spent.
        due = reprocessing_repository.due(connection, run, kind, max(left, 1))
        if not due:
            if not reprocessing_repository.has_open_jobs(connection, kind, run.id):
                reprocessing_repository.finish(connection, [run.id], ReprocessingStatus.DONE)
                logger.info("Reprocessing done", extra={"reprocessing_id": run.id})
            return []
        if left <= 0:
            return []
        job_ids = [
            job_service.enqueue(
                connection, kind, _payload(run.step, row.extraction_id, row.article_id, run.id),
                dedupe_key=reprocessing_repository.dedupe_key(kind, run.id, row.extraction_id), priority=BACKFILL_PRIORITY,
            )
            for row in due
        ]
    return [job_id for job_id in job_ids if job_id is not None]


def retire_replaced(engine: Engine) -> int:
    """The reports of replaced readings leave their events once their article's current reading has all of its
    reports in events: each event they leave records it (event_correction_service.replace_reports). Returns how many
    readings left. The worker calls it once a minute."""
    retired = 0
    with engine.begin() as connection:
        event_repository.lock_assignment(connection)
        for row in reprocessing_repository.replaced_in_events(connection, RETIRE_LIMIT):
            by_event: dict[int, list[int]] = {}
            for report in reprocessing_repository.reports_in_events(connection, row.extraction_id):
                by_event.setdefault(report.event_id, []).append(report.id)
            successor_event_id = reprocessing_repository.first_event(connection, row.current_id)
            for event_id, candidate_ids in by_event.items():
                event_correction_service.replace_reports(connection, event_id, candidate_ids, row.article_id, successor_event_id)
            # IDs only (datamodel.md, rule 13).
            logger.info("Replaced reports left their events", extra={"extraction_id": row.extraction_id, "events": sorted(by_event)})
            retired += 1
    return retired


def article_state(engine: Engine, article_id: int, model: str) -> ArticleState:
    with engine.connect() as connection:
        current = extraction_repository.latest(connection, article_id)
        recognition = entity_repository.recognition_of(connection, current.id) if current else None
    if current is None:
        return ArticleState(None)
    outdated = {}
    for step, run in ((ReprocessingStep.EXTRACTION, current), (ReprocessingStep.RECOGNITION, recognition)):
        # A recognition that asked nothing - the reports name nobody - would ask nothing again.
        if run is None or step == ReprocessingStep.RECOGNITION and not run.names:
            continue
        if (run.schema_version, run.model) != (SCHEMA_VERSIONS[step], model):
            outdated[step] = f"{STEP_LABELS[step]} mit Schema {run.schema_version} und {run.model}"
    return ArticleState(current.id, outdated)


def reprocess_article(engine: Engine, article_id: int, steps: Iterable[str], model: str) -> list[str]:
    """Reprocess the article's current runs of these steps at once, ahead of a reprocessing's jobs - all or none; the
    steps queued. A new extraction brings its own recognition, so with it the recognition needs no job. A step that
    waits or runs already is no error. InvalidReprocessing when a step's run is as one would be made now, or there is
    none."""
    steps = list(dict.fromkeys(steps))
    state = article_state(engine, article_id, model)
    if blocked := [STEP_LABELS.get(step, step) for step in steps if step not in state.outdated]:
        raise InvalidReprocessing(f"nicht neu zu verarbeiten: {', '.join(blocked)}.")
    if ReprocessingStep.EXTRACTION in steps:
        steps = [ReprocessingStep.EXTRACTION]
    with engine.begin() as connection:
        for step in steps:
            kind = JOB_KINDS[step]
            job_service.enqueue(
                connection, kind, _payload(step, state.extraction_id, article_id, None),
                dedupe_key=reprocessing_repository.dedupe_key(kind, None, state.extraction_id), priority=LIVE_PRIORITY,
            )
    logger.info("Article reprocessing queued", extra={"article_id": article_id, "steps": steps})
    return steps


def _payload(step: str, extraction_id: int, article_id: int, reprocessing_id: int | None) -> dict:
    # IDs only (datamodel.md, rule 13).
    payload = {"extraction_id": extraction_id, "reprocessing_id": reprocessing_id}
    return payload | {"article_id": article_id} if step == ReprocessingStep.EXTRACTION else payload


def _parse(connection, form: Mapping[str, str], by_step: dict[str, list[Group]]) -> tuple[Group, Selection, str | None]:
    """The form's choice: one outdated group (Stand), optionally a Quelle and a period of whole days in UTC."""
    key = form.get("group", "")
    group = next((each for groups in by_step.values() for each in groups if each.key == key), None)
    if group is None:
        raise InvalidReprocessing("Stand: bitte einen der aufgeführten wählen.")
    if group.current:
        raise InvalidReprocessing("Stand: dieser ist aktuell, es gibt nichts neu zu verarbeiten.")
    if group.model is None:
        raise InvalidReprocessing("Stand: ohne Modell, nichts neu zu verarbeiten.")
    source_name = None
    raw_source = form.get("source_id", "").strip()
    source_id = None
    if raw_source:
        try:
            source_id = int(raw_source)
        except ValueError:
            raise InvalidReprocessing("Quelle: bitte eine aus der Liste wählen.") from None
        source = source_repository.get(connection, source_id)
        if source is None:
            raise InvalidReprocessing("Quelle nicht gefunden.")
        source_name = source.name
    period_start = _day(form.get("period_start", ""), "Von")
    period_end = _day(form.get("period_end", ""), "Bis")
    if period_start and period_end and period_start > period_end:
        raise InvalidReprocessing("Von: darf nicht nach Bis liegen.")
    selection = Selection(group.step, group.schema_version, group.model, source_name is not None, source_id, period_start, period_end)
    return group, selection, source_name


def _day(raw: str, label: str) -> date | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise InvalidReprocessing(f"{label}: bitte ein Datum wählen.") from None


def _view(row: Row) -> ReprocessingView:
    made = row.extractions_made if row.step == ReprocessingStep.EXTRACTION else row.recognitions_made
    return ReprocessingView(
        row.id, row.step, row.schema_version, row.model, row.source_name, row.period_start, row.period_end, row.status,
        row.total, made, row.open_jobs, row.failed_jobs, row.created_at, row.finished_at,
    )
