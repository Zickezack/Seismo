"""pg_dump and pg_restore calls, with stand-in tools on PATH instead of the real ones."""
import threading
import time

import pytest
from sqlalchemy import make_url

from app.repositories import backup_repository
from app.repositories.backup_repository import ToolFailed, ToolInterrupted

URL = make_url("postgresql+psycopg://seismo:s3cret-pw@db:5432/seismo")


@pytest.fixture
def tool(tmp_path, monkeypatch):
    """Make PATH hold only stand-in shell scripts, so the real pg_dump and pg_restore are never found."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))

    def install(name, script):
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\nPATH=/usr/bin:/bin\n{script}\n")
        path.chmod(0o755)

    return install


def test_dump_gets_the_connection_from_the_environment_and_no_password_in_its_arguments(tool, tmp_path):
    record = tmp_path / "record"
    tool("pg_dump", f'echo "$@" > {record}; echo "$PGHOST $PGPORT $PGUSER $PGPASSWORD $PGDATABASE" >> {record}')

    backup_repository.dump(URL, tmp_path / "out.dump", threading.Event())

    arguments, connection = record.read_text().splitlines()
    assert "s3cret-pw" not in arguments
    assert f"--file={tmp_path / 'out.dump'}" in arguments.split()
    assert "--format=custom" in arguments.split()
    assert connection == "db 5432 seismo s3cret-pw seismo"


def test_restore_loads_everything_or_nothing_into_the_given_database(tool, tmp_path):
    record = tmp_path / "record"
    tool("pg_restore", f'echo "$@" > {record}')

    backup_repository.restore(URL.set(database="seismo_restore"), tmp_path / "in.dump", threading.Event())

    arguments = record.read_text().split()
    assert {"--single-transaction", "--no-owner", "--dbname=seismo_restore"} <= set(arguments)
    assert arguments[-1] == str(tmp_path / "in.dump")


def test_a_failing_tool_raises_with_its_exit_code_and_error_output(tool, tmp_path):
    tool("pg_dump", 'echo "pg_dump: error: connection refused" >&2; exit 3')

    with pytest.raises(ToolFailed) as error:
        backup_repository.dump(URL, tmp_path / "out.dump", threading.Event())

    assert str(error.value) == "pg_dump mit Exit-Code 3 beendet: pg_dump: error: connection refused"


def test_a_missing_tool_names_the_install_script(tool, tmp_path):
    with pytest.raises(ToolFailed, match="pg_dump ist nicht installiert"):
        backup_repository.dump(URL, tmp_path / "out.dump", threading.Event())


def test_a_stopping_worker_ends_a_running_tool(tool, tmp_path):
    started = tmp_path / "started"
    tool("pg_dump", f"touch {started}; exec sleep 30")
    stop_event = threading.Event()
    threading.Thread(target=lambda: (_wait_for(started.exists), stop_event.set()), daemon=True).start()

    begin = time.monotonic()
    with pytest.raises(ToolInterrupted):
        backup_repository.dump(URL, tmp_path / "out.dump", stop_event)

    assert time.monotonic() - begin < 5


def test_files_lists_only_the_given_suffix_and_nothing_for_a_missing_directory(tmp_path):
    (tmp_path / "a.dump").touch()
    (tmp_path / "a.dump.partial").touch()
    (tmp_path / "sub.dump").mkdir()

    assert backup_repository.files(tmp_path, ".dump") == [tmp_path / "a.dump"]
    assert backup_repository.files(tmp_path / "missing", ".dump") == []


def _wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
