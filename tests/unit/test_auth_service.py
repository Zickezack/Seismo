from dataclasses import replace

import pytest

from app.config import AuthSettings
from app.services.auth_service import credentials_match, safe_next_path, session_signing_key

AUTH = AuthSettings(admin_username="admin", admin_password="Grüezi-Mitenand-42", session_secret="s" * 32)


def test_correct_credentials_match():
    assert credentials_match(AUTH, "admin", "Grüezi-Mitenand-42")


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("admin", "Gruezi-Mitenand-42"),
        ("admin", "Grüezi-Mitenand-4"),
        ("admin", "Grüezi-Mitenand-42 "),
        ("Admin", "Grüezi-Mitenand-42"),
        ("", "Grüezi-Mitenand-42"),
        ("admin", ""),
        ("", ""),
    ],
)
def test_wrong_or_empty_credentials_do_not_match(username, password):
    assert not credentials_match(AUTH, username, password)


def test_signing_key_is_stable_for_the_same_settings():
    assert session_signing_key(AUTH) == session_signing_key(replace(AUTH))


@pytest.mark.parametrize(
    "changed",
    [
        {"admin_password": "another-password-1"},
        {"admin_username": "root"},
        {"session_secret": "t" * 32},
    ],
)
def test_changing_credentials_or_secret_changes_the_signing_key(changed):
    # A new key invalidates every cookie signed with the old one, i.e. logs everybody out.
    assert session_signing_key(replace(AUTH, **changed)) != session_signing_key(AUTH)


def test_signing_key_does_not_contain_the_password():
    assert "Grüezi" not in session_signing_key(AUTH)


@pytest.mark.parametrize("path", ["/", "/events", "/events?country=CH&page=2", "/login?next=%2F"])
def test_paths_on_this_site_are_kept(path):
    assert safe_next_path(path) == path


@pytest.mark.parametrize(
    "target",
    [
        None,
        "",
        "events",
        "https://evil.example/",
        "//evil.example",
        "/\\evil.example",
        "/\t/evil.example",
        "/\n/evil.example",
        "javascript:alert(1)",
    ],
)
def test_other_hosts_and_invalid_targets_go_to_the_start_page(target):
    assert safe_next_path(target) == "/"
