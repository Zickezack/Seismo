from sqlalchemy import Connection, inspect, text

# Alembic's default version table. Read directly: Alembic's MigrationContext would log two INFO
# lines on every readiness check.
VERSION_TABLE = "alembic_version"


def current_revision(connection: Connection) -> str | None:
    """The Alembic revision the database is at, or None before the first migration."""
    if not inspect(connection).has_table(VERSION_TABLE):
        return None
    return connection.execute(text(f"SELECT version_num FROM {VERSION_TABLE}")).scalar_one_or_none()
