import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(not (REPO_ROOT / ".git").exists(), reason="not a Git checkout")

IGNORED = [
    ".env",
    ".env.local",
    ".env.production",
    "deploy/server.key",
    "deploy/server.pem",
    ".claude/settings.local.json",
    "CLAUDE.local.md",
    "docker-compose.override.yml",
    ".vscode/settings.json",
    "app/__pycache__/config.cpython-314.pyc",
    "example.egg-info/PKG-INFO",
    "seismo-20261023T030000Z.dump",
    "backups/seismo-20261023T030000Z.dump.partial",
]

VERSIONED = [
    ".env.example",
    ".gitignore",
    ".gitattributes",
    ".claude/skills/neues-feature/SKILL.md",
    ".claude/hooks/next_story.py",
    ".devcontainer/devcontainer.json",
    "docker-compose.yml",
    "app/config.py",
    "scripts/install-postgres-client.sh",
]


def git(*args):
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)


def is_ignored(path):
    # --no-index: check the rules themselves, also for paths that are already tracked.
    result = git("check-ignore", "--no-index", "--quiet", path)
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


@pytest.mark.parametrize("path", IGNORED)
def test_secrets_and_local_settings_are_ignored(path):
    assert is_ignored(path)


@pytest.mark.parametrize("path", VERSIONED)
def test_project_files_are_versioned(path):
    assert not is_ignored(path)


def test_no_tracked_file_matches_an_ignore_rule():
    # Catches files added despite the rules, e.g. with `git add -f .env`.
    result = git("ls-files", "--cached", "--ignored", "--exclude-standard")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == []
