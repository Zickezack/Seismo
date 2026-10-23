"""Job queue on PostgreSQL (userstories.md, Projektfundament und Laufzeitumgebung, story 4)."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.models import Job, JobStatus
from app.jobs import runner
from app.jobs.errors import JobInterrupted, PermanentJobError
from app.services import job_service, platform_settings_service
from tests.integration.alembic_helpers import upgrade

KIND = "integration-test"
FOLLOW_UP = "follow-up"


@pytest.fixture
def queue(engine):
    upgrade(engine)
    return engine


def enqueue(engine, kind=KIND, **options):
    with engine.begin() as connection:
        return job_service.enqueue(connection, kind, **options)


def claim(engine, owner="worker-a", kinds=(KIND,)):
    with engine.begin() as connection:
        return job_service.claim_next(connection, kinds, owner)


def row(engine, job_id):
    with engine.connect() as connection:
        return connection.execute(select(Job.__table__).where(Job.id == job_id)).one()


def rows_of_kind(engine, kind):
    with engine.connect() as connection:
        return connection.execute(select(Job.__table__).where(Job.kind == kind)).all()


def set_in_past(engine, job_id, column):
    """Jump the clock instead of waiting: lease_expires_at or run_after one second ago."""
    with engine.begin() as connection:
        connection.execute(update(Job).where(Job.id == job_id).values({column: func.now() - timedelta(seconds=1)}))


def seconds_until(engine, job_id, column):
    with engine.connect() as connection:
        return connection.execute(select(func.extract("epoch", getattr(Job, column) - func.now())).where(Job.id == job_id)).scalar_one()


def enqueue_follow_up(connection):
    """An effect with a visible result: it adds a job of another kind."""
    job_service.enqueue(connection, FOLLOW_UP)


def failing(error):
    def handler(_job):
        raise error

    return handler


def make_failed_job(engine, **options):
    job_id = enqueue(engine, max_attempts=1, **options)
    runner.execute(engine, failing(PermanentJobError("kaputt")), claim(engine))
    assert row(engine, job_id).status == JobStatus.FAILED
    return job_id


def test_job_runs_once_and_its_effect_commits_with_it(queue):
    job_id = enqueue(queue, payload={"article_id": 7})

    job = claim(queue)
    running = row(queue, job_id)
    assert (job.id, job.payload, job.attempts) == (job_id, {"article_id": 7}, 1)
    assert running.status == JobStatus.RUNNING
    assert running.lease_owner == "worker-a"
    assert seconds_until(queue, job_id, "lease_expires_at") == pytest.approx(job_service.LEASE_SECONDS, abs=5)

    runner.execute(queue, lambda _job: enqueue_follow_up, job)

    done = row(queue, job_id)
    assert done.status == JobStatus.SUCCEEDED
    assert done.finished_at is not None
    assert (done.lease_token, done.lease_owner, done.lease_expires_at) == (None, None, None)
    assert len(rows_of_kind(queue, FOLLOW_UP)) == 1
    assert claim(queue) is None


def test_concurrent_workers_never_claim_the_same_job(queue):
    first, second = enqueue(queue), enqueue(queue)

    with queue.connect() as worker_a, queue.connect() as worker_b, queue.connect() as worker_c:
        # Worker a has claimed but not committed yet: its row stays locked, the others skip it.
        job_a = job_service.claim_next(worker_a, [KIND], "worker-a")
        job_b = job_service.claim_next(worker_b, [KIND], "worker-b")
        job_c = job_service.claim_next(worker_c, [KIND], "worker-c")
        worker_a.commit()
        worker_b.commit()

    assert {job_a.id, job_b.id} == {first, second}
    assert job_c is None


def test_job_of_a_vanished_worker_is_taken_over_once_its_lease_expires(queue):
    job_id = enqueue(queue)
    claim(queue, owner="worker-a")

    assert claim(queue, owner="worker-b") is None

    set_in_past(queue, job_id, "lease_expires_at")
    job = claim(queue, owner="worker-b")

    assert (job.id, job.attempts) == (job_id, 2)
    taken_over = row(queue, job_id)
    assert taken_over.lease_owner == "worker-b"
    assert taken_over.last_error == job_service.LEASE_EXPIRED_ERROR


def test_expired_lease_on_the_last_attempt_fails_the_job(queue):
    job_id = enqueue(queue, max_attempts=1)
    claim(queue)
    set_in_past(queue, job_id, "lease_expires_at")

    assert claim(queue) is None
    failed = row(queue, job_id)
    assert failed.status == JobStatus.FAILED
    assert failed.last_error == job_service.LEASE_EXPIRED_ERROR
    assert failed.finished_at is not None


def test_worker_that_lost_its_lease_writes_nothing(queue):
    job_id = enqueue(queue)
    stale = claim(queue, owner="worker-a")
    set_in_past(queue, job_id, "lease_expires_at")
    current = claim(queue, owner="worker-b")

    # Worker a comes back after the takeover: neither its result nor its failure is written.
    runner.execute(queue, lambda _job: enqueue_follow_up, stale)
    runner.execute(queue, failing(RuntimeError("zu spät")), stale)

    assert rows_of_kind(queue, FOLLOW_UP) == []
    assert row(queue, job_id).status == JobStatus.RUNNING
    assert row(queue, job_id).lease_owner == "worker-b"

    runner.execute(queue, lambda _job: enqueue_follow_up, current)

    assert row(queue, job_id).status == JobStatus.SUCCEEDED
    assert len(rows_of_kind(queue, FOLLOW_UP)) == 1


def test_failing_effect_writes_nothing_and_the_job_is_retried(queue):
    job_id = enqueue(queue)

    def broken_effect(connection):
        enqueue_follow_up(connection)
        raise RuntimeError("Schreiben fehlgeschlagen")

    runner.execute(queue, lambda _job: broken_effect, claim(queue))

    assert rows_of_kind(queue, FOLLOW_UP) == []
    assert row(queue, job_id).status == JobStatus.QUEUED
    assert row(queue, job_id).last_error == "RuntimeError: Schreiben fehlgeschlagen"


def test_failed_attempt_is_retried_with_a_delay_until_the_attempts_run_out(queue):
    job_id = enqueue(queue, max_attempts=2)

    runner.execute(queue, failing(RuntimeError("Zeitüberschreitung")), claim(queue))

    retry = row(queue, job_id)
    assert (retry.status, retry.attempts, retry.lease_token) == (JobStatus.QUEUED, 1, None)
    assert retry.last_error == "RuntimeError: Zeitüberschreitung"
    assert seconds_until(queue, job_id, "run_after") == pytest.approx(job_service.retry_delay_seconds(1), abs=5)
    assert claim(queue) is None

    set_in_past(queue, job_id, "run_after")
    job = claim(queue)
    assert job.attempts == 2
    runner.execute(queue, failing(RuntimeError("wieder")), job)

    failed = row(queue, job_id)
    assert (failed.status, failed.attempts, failed.last_error) == (JobStatus.FAILED, 2, "RuntimeError: wieder")
    assert failed.finished_at is not None


def test_permanent_error_fails_without_retry(queue):
    job_id = enqueue(queue, max_attempts=5)

    runner.execute(queue, failing(PermanentJobError("Artikel existiert nicht mehr")), claim(queue))

    assert row(queue, job_id).status == JobStatus.FAILED
    assert row(queue, job_id).attempts == 1


def test_interrupted_job_goes_back_without_counting_the_attempt(queue):
    job_id = enqueue(queue)

    runner.execute(queue, failing(JobInterrupted()), claim(queue))

    released = row(queue, job_id)
    assert (released.status, released.attempts, released.lease_token) == (JobStatus.QUEUED, 0, None)
    assert claim(queue).attempts == 1


def test_dedupe_key_keeps_a_job_from_waiting_twice(queue):
    job_id = enqueue(queue, dedupe_key="article:7")

    assert enqueue(queue, dedupe_key="article:7") is None
    assert enqueue(queue, dedupe_key="article:8") is not None

    runner.execute(queue, lambda _job: None, claim(queue))
    assert row(queue, job_id).status == JobStatus.SUCCEEDED

    # Once done, the same work may be queued again (e.g. reprocessing after a model change).
    assert enqueue(queue, dedupe_key="article:7") not in (None, job_id)


def test_dedupe_works_for_many_jobs_on_one_connection(queue):
    # From the 11th run on one connection PostgreSQL plans the prepared insert generically; with the index
    # predicate as bound parameters ON CONFLICT then found no matching index (found with 13 due sources).
    with queue.begin() as connection:
        first = [job_service.enqueue(connection, KIND, dedupe_key=f"source:{n}") for n in range(20)]
        again = [job_service.enqueue(connection, KIND, dedupe_key=f"source:{n}") for n in range(20)]

    assert all(first) and len(set(first)) == 20
    assert again == [None] * 20


def test_only_due_jobs_of_the_worker_kinds_are_claimed(queue):
    enqueue(queue, kind="other-kind")

    assert claim(queue) is None
    assert claim(queue, kinds=()) is None
    assert claim(queue, kinds=("other-kind",)) is not None


def test_lease_is_renewed_while_the_handler_runs(queue):
    job_id = enqueue(queue)
    job = claim(queue)
    set_in_past(queue, job_id, "lease_expires_at")
    renewals = []

    with runner.keep_lease(queue, job, interval=0.02, on_renewed=lambda: renewals.append(1)):
        while not renewals:
            pass

    assert seconds_until(queue, job_id, "lease_expires_at") > job_service.LEASE_SECONDS - 5
    assert claim(queue, owner="worker-b") is None


def test_requeue_failed_is_all_or_nothing(queue):
    failed_a, failed_b = make_failed_job(queue), make_failed_job(queue)
    running = enqueue(queue)
    claim(queue)

    result = job_service.requeue_failed(queue, [failed_a, running, 999_999])

    assert result == job_service.BulkResult(
        processed=0, blocked=[f"Job #{running} (Läuft)", "Job #999999 (nicht gefunden)"]
    )
    assert row(queue, failed_a).status == JobStatus.FAILED

    result = job_service.requeue_failed(queue, [failed_b, failed_a, failed_a])

    assert result == job_service.BulkResult(processed=2, blocked=[])
    for job_id in (failed_a, failed_b):
        requeued = row(queue, job_id)
        assert (requeued.status, requeued.attempts, requeued.finished_at) == (JobStatus.QUEUED, 0, None)
        # The error stays visible until the next attempt.
        assert requeued.last_error == "PermanentJobError: kaputt"


def test_requeue_is_blocked_while_the_same_work_is_queued(queue):
    failed = make_failed_job(queue, dedupe_key="article:7")
    duplicate = make_failed_job(queue, dedupe_key="article:7")

    assert job_service.requeue_failed(queue, [failed, duplicate]).blocked == [
        f"Job #{duplicate} (gleicher Job mehrfach ausgewählt)"
    ]

    enqueue(queue, dedupe_key="article:7")

    assert job_service.requeue_failed(queue, [failed]).blocked == [
        f"Job #{failed} (gleicher Job wartet oder läuft bereits)"
    ]


def test_overview_counts_every_status_and_lists_the_newest_first(queue):
    failed = make_failed_job(queue)
    succeeded = enqueue(queue)
    runner.execute(queue, lambda _job: None, claim(queue))
    queued = enqueue(queue, kind="other-kind")

    overview = job_service.overview(queue, None)

    assert overview.counts == {JobStatus.QUEUED: 1, JobStatus.RUNNING: 0, JobStatus.SUCCEEDED: 1, JobStatus.FAILED: 1}
    assert [job.id for job in overview.jobs] == [queued, succeeded, failed]
    assert (overview.llm_slots_in_use, overview.llm_max_parallel_calls) == (0, 1)

    only_failed = job_service.overview(queue, "failed")
    assert [job.id for job in only_failed.jobs] == [failed]
    assert only_failed.jobs[0].can_requeue
    assert only_failed.matching == 1

    assert job_service.overview(queue, "no-such-status").status is None


def test_finished_jobs_are_purged_after_their_retention_period(queue):
    platform_settings_service.save(queue, {"succeeded_jobs_retention_days": "7", "failed_jobs_retention_days": "30"})
    old_success, recent_success = enqueue(queue), enqueue(queue)
    for _ in range(2):
        runner.execute(queue, lambda _job: None, claim(queue))
    old_failure, recent_failure = make_failed_job(queue), make_failed_job(queue)
    waiting = enqueue(queue)
    with queue.begin() as connection:
        for job_id, days in ((old_success, 8), (recent_success, 6), (old_failure, 31), (recent_failure, 29)):
            connection.execute(update(Job).where(Job.id == job_id).values(finished_at=func.now() - timedelta(days=days)))
        # A waiting job is never finished, however old it is.
        connection.execute(update(Job).where(Job.id == waiting).values(created_at=func.now() - timedelta(days=400)))

    deleted = job_service.purge_finished_jobs(queue)

    assert deleted == {JobStatus.SUCCEEDED: 1, JobStatus.FAILED: 1}
    with queue.connect() as connection:
        remaining = set(connection.execute(select(Job.id)).scalars())
    assert remaining == {recent_success, recent_failure, waiting}
