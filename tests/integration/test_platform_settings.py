"""Plattform-Einstellungen in PostgreSQL: defaults, a single row, saving."""
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.models import PlatformSettingsRecord
from app.repositories import platform_settings_repository
from app.services import platform_settings_service
from app.services.platform_settings_service import InvalidSetting, PlatformSettings
from tests.integration.alembic_helpers import upgrade


@pytest.fixture
def database(engine):
    upgrade(engine)
    return engine


def test_defaults_apply_while_nothing_is_saved(database):
    assert platform_settings_service.current(database) == PlatformSettings(
        llm_max_parallel_calls=1, succeeded_jobs_retention_days=7, failed_jobs_retention_days=30
    )


def test_saving_twice_keeps_a_single_row(database):
    platform_settings_service.save(database, {"llm_max_parallel_calls": "3"})
    saved = platform_settings_service.save(database, {"llm_max_parallel_calls": "4"})

    assert saved.llm_max_parallel_calls == 4
    assert platform_settings_service.current(database).llm_max_parallel_calls == 4
    with database.connect() as connection:
        assert connection.execute(select(func.count()).select_from(PlatformSettingsRecord)).scalar_one() == 1


def test_each_section_saves_only_its_own_settings(database):
    platform_settings_service.save(database, {"llm_max_parallel_calls": "2"})
    platform_settings_service.save(database, {"succeeded_jobs_retention_days": "3", "failed_jobs_retention_days": "60"})

    assert platform_settings_service.current(database) == PlatformSettings(
        llm_max_parallel_calls=2, succeeded_jobs_retention_days=3, failed_jobs_retention_days=60
    )


def test_one_invalid_value_changes_nothing(database):
    platform_settings_service.save(database, {"succeeded_jobs_retention_days": "3"})

    with pytest.raises(InvalidSetting):
        platform_settings_service.save(database, {"succeeded_jobs_retention_days": "5", "failed_jobs_retention_days": "0"})

    assert platform_settings_service.current(database).succeeded_jobs_retention_days == 3


def test_database_rejects_a_limit_below_one(database):
    with pytest.raises(IntegrityError), database.begin() as connection:
        platform_settings_repository.save(connection, {"llm_max_parallel_calls": 0})
