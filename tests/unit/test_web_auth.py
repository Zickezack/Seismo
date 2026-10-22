"""Admin login (userstories.md, Projektfundament und Laufzeitumgebung, story 3).

The whole web stack runs, without a database: the login never touches it.
"""
import logging
import re
from dataclasses import replace

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from starlette.routing import Mount

from app.config import AuthSettings, Settings
from app.web.access import SESSION_COOKIE
from app.web.main import create_app
from app.web.routes import auth as auth_routes

SETTINGS = Settings(postgres_user="u", postgres_password="p", postgres_db="d")
AUTH = AuthSettings(admin_username="admin", admin_password="correct horse battery", session_secret="s" * 32)
CREDENTIALS = {"username": "admin", "password": "correct horse battery"}

# The only routes reachable without a login. Adding one here is a security decision.
PUBLIC_PATHS = {"/health/live", "/health/ready", "/login", "/logout"}
BROWSER = {"Accept": "text/html,application/xhtml+xml"}


def make_client(auth=AUTH):
    return TestClient(create_app(SETTINGS, auth), follow_redirects=False)


@pytest.fixture
def client():
    with make_client() as client:
        yield client


def login(client, **form):
    return client.post("/login", data=CREDENTIALS | form)


def text_of(html, testid):
    """Text of the element with this data-testid - never a search through the whole page."""
    match = re.search(rf'data-testid="{testid}"[^>]*>([^<]*)<', html)
    return match.group(1).strip() if match else None


def test_login_flow_end_to_end(client):
    # A browser opening a page without a session lands on the login page, with the way back.
    response = client.get("/", headers=BROWSER)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F"

    response = client.get("/login?next=%2F")
    assert response.status_code == 200
    assert '<input type="hidden" name="next" value="/">' in response.text

    # A wrong password shows the form again, with an error and without a session.
    response = login(client, password="wrong", next="/")
    assert response.status_code == 200
    assert text_of(response.text, "login-error") == "Benutzername oder Passwort ist falsch."
    assert SESSION_COOKIE not in client.cookies

    response = login(client, next="/")
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    response = client.get("/")
    assert response.status_code == 200
    assert text_of(response.text, "session-user") == "Angemeldet als admin"

    response = client.post("/logout")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get("/", headers=BROWSER).status_code == 303


def test_every_route_except_the_public_ones_requires_login(client):
    checked = []
    # The routes as served, i.e. with the prefixes of the routers that include them.
    for route in iter_route_contexts(client.app.routes):
        if isinstance(route.original_route, Mount):
            assert route.path == "/static", f"unexpected mount {route.path}"
            continue
        # A plain Starlette route would bypass the router dependencies, require_admin included.
        assert isinstance(route.original_route, APIRoute), f"{route.path} is not an APIRoute"
        if route.path in PUBLIC_PATHS:
            continue
        for method in route.methods:
            response = client.request(method, route.path)
            assert response.status_code == 401, f"{method} {route.path} answers without a login"
            checked.append(route.path)

    assert "/" in checked


def test_health_checks_login_page_and_stylesheet_are_public(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/static/style.css").status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_are_switched_off(client, path):
    assert client.get(path).status_code == 404


def test_redirect_to_login_keeps_the_query_string(client):
    response = client.get("/?country=CH", headers=BROWSER)

    assert response.headers["location"] == "/login?next=%2F%3Fcountry%3DCH"


def test_login_never_redirects_to_another_host(client):
    response = login(client, next="//evil.example/")

    assert response.headers["location"] == "/"


def test_login_page_sends_a_logged_in_admin_on(client):
    login(client)

    response = client.get("/login?next=%2Fevents")

    assert response.status_code == 303
    assert response.headers["location"] == "/events"


def test_session_cookie_is_http_only_and_same_site_lax(client):
    cookie = login(client).headers["set-cookie"].lower()

    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "; secure" not in cookie


def test_session_cookie_is_secure_when_configured():
    with make_client(replace(AUTH, session_cookie_secure=True)) as client:
        cookie = login(client).headers["set-cookie"].lower()

    assert "; secure" in cookie


def test_session_survives_an_api_restart(client):
    login(client)

    with make_client() as restarted:
        restarted.cookies.set(SESSION_COOKIE, client.cookies[SESSION_COOKIE])
        assert restarted.get("/").status_code == 200


def test_changing_the_admin_password_logs_out_open_sessions(client):
    login(client)

    with make_client(replace(AUTH, admin_password="a brand new password")) as restarted:
        restarted.cookies.set(SESSION_COOKIE, client.cookies[SESSION_COOKIE])
        assert restarted.get("/").status_code == 401


def test_failed_login_logs_neither_username_nor_password(client, caplog):
    with caplog.at_level(logging.INFO, logger=auth_routes.__name__):
        login(client, username="typed-user", password="typed-password")

    [record] = [record for record in caplog.records if record.name == auth_routes.__name__]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "Login failed"
    assert "typed-user" not in str(record.__dict__)
    assert "typed-password" not in str(record.__dict__)


def test_username_is_escaped_on_the_login_page(client):
    response = login(client, username="<script>alert(1)</script>", password="wrong")

    assert "<script>alert(1)</script>" not in response.text
    assert 'value="&lt;script&gt;alert(1)&lt;/script&gt;"' in response.text
