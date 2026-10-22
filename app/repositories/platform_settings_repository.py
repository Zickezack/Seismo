"""Data access for the Plattform-Einstellungen: a single row for the whole instance."""
from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import PlatformSettingsRecord

SINGLE_ROW_ID = 1


def load(connection: Connection) -> dict | None:
    """The stored values, or None while nothing has been saved yet."""
    columns = [column for column in PlatformSettingsRecord.__table__.columns if column.key not in ("id", "updated_at")]
    row = connection.execute(select(*columns).where(PlatformSettingsRecord.id == SINGLE_ROW_ID)).one_or_none()
    return dict(row._mapping) if row else None


def save(connection: Connection, values: dict) -> None:
    statement = insert(PlatformSettingsRecord).values(id=SINGLE_ROW_ID, **values)
    statement = statement.on_conflict_do_update(
        index_elements=[PlatformSettingsRecord.id], set_={**values, "updated_at": func.now()}
    )
    connection.execute(statement)
