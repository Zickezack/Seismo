from app.db.migrations import head_revision
from app.repositories.schema_repository import current_revision
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
