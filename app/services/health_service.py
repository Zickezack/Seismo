"""Readiness rules shared by the api and the worker."""
import logging
from dataclasses import dataclass

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.repositories import schema_repository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthReport:
    database_reachable: bool
    schema_current: bool

    @property
    def ready(self) -> bool:
        return self.database_reachable and self.schema_current


def check_readiness(engine: Engine, expected_revision: str) -> HealthReport:
    """Ready means: the database answers and its schema is at the newest migration in the code."""
    try:
        with engine.connect() as connection:
            revision = schema_repository.current_revision(connection)
    except SQLAlchemyError as error:
        logger.warning("Database unreachable: %s", getattr(error, "orig", None) or error)
        return HealthReport(database_reachable=False, schema_current=False)
    return HealthReport(database_reachable=True, schema_current=revision == expected_revision)
