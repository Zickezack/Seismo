"""Backups from the command line (README.md, "Backups"):

    python -m app.backup list               the backups in BACKUP_DIR, newest first
    python -m app.backup create             a backup now, besides the automatic ones
    python -m app.backup restore <file>     replace the database with a backup

Runs in the worker container, which has pg_dump, pg_restore and the backup volume.
"""
import argparse
import logging
import sys
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db.migrations import head_revision
from app.db.session import create_db_engine
from app.logging_config import configure_logging
from app.repositories.backup_repository import ToolFailed
from app.services import backup_service
from app.services.backup_service import RestoreError


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    settings = Settings.from_env()
    # Problems only: each command prints its own result.
    configure_logging("backup", logging.WARNING)
    engine = create_db_engine(settings)
    try:
        return arguments.command(engine, settings.backup_dir, arguments)
    except (RestoreError, ToolFailed, OSError) as error:
        print(f"Fehler: {error}", file=sys.stderr)
    except SQLAlchemyError as error:
        print(f"Fehler in der Datenbank: {getattr(error, 'orig', None) or error}", file=sys.stderr)
    except KeyboardInterrupt:
        print("Abgebrochen.", file=sys.stderr)
    finally:
        engine.dispose()
    return 1


def list_backups(engine: Engine, backup_dir: Path, _arguments) -> int:
    backups = backup_service.list_backups(backup_dir, engine.url.database)
    if not backups:
        print(f"Keine Backups in {backup_dir}.")
    for backup in backups:
        print(f"{backup.path.name}  {_size(backup.size_bytes):>10}  {backup.created_at:%Y-%m-%d %H:%M:%S} UTC")
    return 0


def create_backup(engine: Engine, backup_dir: Path, _arguments) -> int:
    backup = backup_service.create_backup(engine, backup_dir)
    print(f"Backup erstellt: {backup.path} ({_size(backup.size_bytes)})")
    return 0


def restore(engine: Engine, backup_dir: Path, arguments) -> int:
    source = Path(arguments.file)
    if not source.is_absolute():
        source = backup_dir / source
    database = engine.url.database
    if not arguments.yes and not _confirmed(source, database):
        print("Nicht bestätigt, nichts geändert.")
        return 1
    result = backup_service.restore(engine, source)
    print(f'Wiederhergestellt: Datenbank "{database}" aus {source.name}.')
    print(f"Schema-Version des Backups: {result.revision}, dieser Code: {head_revision()}.")
    if result.previous_database:
        print(f'Die bisherige Datenbank heisst jetzt "{result.previous_database}" - löschen, sobald sie nicht mehr gebraucht wird.')
    print("Jetzt api und worker starten; migrate bringt ein älteres Schema dabei auf den neuesten Stand.")
    return 0


def _confirmed(source: Path, database: str) -> bool:
    print(f'Der Restore ersetzt die Datenbank "{database}" durch das Backup {source.name}.')
    print("Die bisherige Datenbank bleibt unter neuem Namen erhalten. api und worker vorher stoppen.")
    try:
        return input("Zum Bestätigen den Datenbanknamen eingeben: ").strip() == database
    except EOFError:
        return False


def _size(size_bytes: int) -> str:
    return f"{size_bytes / 1024 / 1024:.1f} MB"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.backup", description="Datenbank-Backups von Seismo.")
    commands = parser.add_subparsers(required=True, metavar="Befehl")
    commands.add_parser("list", help="Backups auflisten, neuestes zuerst").set_defaults(command=list_backups)
    commands.add_parser("create", help="jetzt ein Backup erstellen").set_defaults(command=create_backup)
    restore_parser = commands.add_parser("restore", help="Datenbank durch ein Backup ersetzen")
    restore_parser.add_argument("file", help="Dateiname aus 'list' oder absoluter Pfad")
    restore_parser.add_argument("--yes", action="store_true", help="ohne Rückfrage")
    restore_parser.set_defaults(command=restore)
    return parser
