"""Data access for database backups: pg_dump and pg_restore, backup files, whole databases.

pg_dump and pg_restore come from scripts/install-postgres-client.sh. They get the connection through
PG* environment variables, so the password never shows up in a process list.
"""
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import URL, Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from app.db.session import CONNECT_TIMEOUT_SECONDS
from app.repositories import schema_repository

# How often a running tool checks whether the worker is stopping.
STOP_CHECK_SECONDS = 0.5
# Docker kills a container 10 s after asking it to stop; the tool gets part of that to end on its own.
TERMINATE_TIMEOUT_SECONDS = 5
MAX_TOOL_ERROR_LENGTH = 1000
# Every server has it; whole databases are created, renamed and dropped from there.
MAINTENANCE_DATABASE = "postgres"
# SQLSTATE object_in_use: "database is being accessed by other users".
OBJECT_IN_USE = "55006"


class ToolFailed(RuntimeError):
    """pg_dump or pg_restore ended with an error; the message holds its own error output."""


class ToolInterrupted(Exception):
    """The worker is stopping; the tool was ended before it finished."""


class DatabaseInUse(Exception):
    """A session connected to the database between ending its sessions and renaming it."""


def dump(url: URL, target: Path, stop_event: threading.Event) -> None:
    """Write the database at `url` into `target`, in pg_dump's compressed custom format."""
    # A lock held by a migration would otherwise stall the dump without end; the job retries instead.
    _run(["pg_dump", "--format=custom", "--lock-wait-timeout=60s", f"--file={target}"], url, stop_event)


def restore(url: URL, source: Path, stop_event: threading.Event) -> None:
    """Load `source` into the empty database at `url`, all or nothing."""
    # Without owners and grants, a backup also loads under a different POSTGRES_USER.
    arguments = ["pg_restore", "--single-transaction", "--no-owner", "--no-privileges", f"--dbname={url.database}"]
    _run([*arguments, str(source)], url, stop_event)


def files(directory: Path, suffix: str) -> list[Path]:
    """The files in `directory` ending in `suffix`; none while the directory does not exist."""
    if not directory.is_dir():
        return []
    return [path for path in directory.iterdir() if path.name.endswith(suffix) and path.is_file()]


def free_bytes(directory: Path) -> int | None:
    """Free space on the file system of `directory`; None while it cannot be read."""
    try:
        return shutil.disk_usage(directory).free
    except OSError:
        return None


class NotEnoughSpace(OSError):
    """Copying stopped: the file system would keep less free than asked for."""


# Copied in steps of this size, the free space checked after each.
COPY_CHUNK_BYTES = 1024 * 1024


def save(source: BinaryIO, target: Path, keep_free: int = 0) -> None:
    """Copy an open file into `target`, which must not exist yet. Stops with NotEnoughSpace once less than `keep_free`
    bytes are left on the file system; the part copied stays, for the caller to delete."""
    with target.open("xb") as file:
        while chunk := source.read(COPY_CHUNK_BYTES):
            file.write(chunk)
            if keep_free and (free_bytes(target.parent) or 0) < keep_free:
                raise NotEnoughSpace(f"less than {keep_free} bytes free")


def delete(paths: Iterable[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def maintenance_engine(url: URL) -> Engine:
    """An engine on the maintenance database; CREATE and DROP DATABASE need autocommit."""
    return create_engine(
        url.set(database=MAINTENANCE_DATABASE),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )


def revision_of(url: URL) -> str | None:
    """The Alembic revision of the database at `url`, or None if it has none."""
    engine = create_engine(url, connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS})
    try:
        with engine.connect() as connection:
            return schema_repository.current_revision(connection)
    finally:
        engine.dispose()


def database_exists(connection: Connection, name: str) -> bool:
    return connection.execute(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}).first() is not None


def create_database(connection: Connection, name: str) -> None:
    connection.execute(text(f"CREATE DATABASE {_quote(connection, name)}"))


def drop_database(connection: Connection, name: str) -> None:
    connection.execute(text(f"DROP DATABASE IF EXISTS {_quote(connection, name)} WITH (FORCE)"))


def swap_databases(connection: Connection, current: str, replacement: str, previous: str | None) -> None:
    """Give `replacement` the name `current`, which moves to `previous` - in one transaction, so a
    failure leaves both names as they were. Without `previous`, `current` does not exist yet."""
    # A real transaction: on the autocommit connection of maintenance_engine every statement counts at once.
    connection = connection.execution_options(isolation_level="READ COMMITTED")
    try:
        with connection.begin():
            if previous is not None:
                # Renaming needs a database nobody is connected to.
                connection.execute(
                    text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :name AND pid <> pg_backend_pid()"),
                    {"name": current},
                )
                connection.execute(text(f"ALTER DATABASE {_quote(connection, current)} RENAME TO {_quote(connection, previous)}"))
            connection.execute(text(f"ALTER DATABASE {_quote(connection, replacement)} RENAME TO {_quote(connection, current)}"))
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) == OBJECT_IN_USE:
            raise DatabaseInUse(current) from error
        raise


def _quote(connection: Connection, name: str) -> str:
    return connection.dialect.identifier_preparer.quote(name)


def _run(arguments: list[str], url: URL, stop_event: threading.Event) -> None:
    environment = os.environ | {
        "PGHOST": url.host or "",
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username or "",
        "PGPASSWORD": url.password or "",
        "PGDATABASE": url.database or "",
        "PGCONNECT_TIMEOUT": str(CONNECT_TIMEOUT_SECONDS),
    }
    # A file rather than a pipe: a pipe nobody reads blocks the tool once it is full.
    with tempfile.TemporaryFile() as error_output:
        try:
            process = subprocess.Popen(
                arguments, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=error_output
            )
        except FileNotFoundError:
            raise ToolFailed(f"{arguments[0]} ist nicht installiert (scripts/install-postgres-client.sh)") from None
        try:
            while process.poll() is None:
                if stop_event.wait(STOP_CHECK_SECONDS):
                    raise ToolInterrupted(f"{arguments[0]} beendet, weil der Worker stoppt")
        finally:
            _end(process)
        if process.returncode != 0:
            error_output.seek(0)
            message = error_output.read().decode(errors="replace").strip()[-MAX_TOOL_ERROR_LENGTH:]
            raise ToolFailed(f"{arguments[0]} mit Exit-Code {process.returncode} beendet: {message or 'keine Ausgabe'}")


def _end(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(TERMINATE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
