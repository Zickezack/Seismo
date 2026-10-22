from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

import app.db.models  # noqa: F401 - registers the tables on Base.metadata
from app.db.base import Base
from app.db.migrations import head_revision
from app.repositories import platform_settings_repository
from app.repositories.schema_repository import current_revision
from app.services import job_service
from tests.integration.alembic_helpers import downgrade, upgrade


def revision_of(engine):
    with engine.connect() as connection:
        return current_revision(connection)


def test_upgrade_downgrade_and_upgrade_again(engine):
    assert revision_of(engine) is None

    upgrade(engine)
    assert revision_of(engine) == head_revision()

    downgrade(engine)
    assert revision_of(engine) is None

    upgrade(engine)
    assert revision_of(engine) == head_revision()


def test_migrated_schema_matches_the_models(engine):
    upgrade(engine)

    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def test_downgrade_and_upgrade_with_rows_in_place(engine):
    upgrade(engine)
    with engine.begin() as connection:
        job_service.enqueue(connection, "testjob", dedupe_key="article:1")
        platform_settings_repository.save(connection, {"llm_max_parallel_calls": 2})

    downgrade(engine)
    upgrade(engine)

    assert revision_of(engine) == head_revision()
