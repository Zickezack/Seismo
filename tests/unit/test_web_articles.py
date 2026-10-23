"""Artikel page in the web layer, without a database: the service is replaced."""
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import AuthSettings, Settings
from app.services import article_service
from app.services.article_service import ArticleListItem, ArticleOverview, SourceChoice
from app.web.main import create_app

SETTINGS = Settings(postgres_user="u", postgres_password="p", postgres_db="d")
AUTH = AuthSettings(admin_username="admin", admin_password="correct horse battery", session_secret="s" * 32)
NOW = datetime(2026, 10, 23, 9, 30, 0, tzinfo=UTC)


def article(article_id, **fields):
    values = dict(
        id=article_id, url=f"https://example.org/{article_id}.html", title=f"Meldung <{article_id}>", published_at=NOW,
        fetched_at=NOW, language="de", source_id=2, source_display_name="EZB", source_exists=True,
    )
    return ArticleListItem(**values | fields)


OVERVIEW = ArticleOverview(
    source_id=None,
    sources=[SourceChoice(2, "EZB"), SourceChoice(3, "Fed")],
    articles=[
        article(11),
        article(10, title=None, published_at=None, language=None, source_id=None, source_display_name="Alt", source_exists=False),
    ],
    matching=2,
)


@pytest.fixture
def client():
    with TestClient(create_app(SETTINGS, AUTH), follow_redirects=False) as client:
        client.post("/login", data={"username": "admin", "password": "correct horse battery"})
        yield client


@pytest.fixture
def overview(monkeypatch):
    calls = {"overview": OVERVIEW}

    def fake(_engine, source):
        calls["source"] = source
        return calls["overview"]

    monkeypatch.setattr(article_service, "overview", fake)
    return calls


def text_of(html, testid):
    match = re.search(rf'data-testid="{testid}"[^>]*>([^<]*)<', html)
    return match.group(1).strip() if match else None


def tag_of(html, testid):
    match = re.search(rf'<[^>]*data-testid="{testid}"[^>]*>', html)
    return match.group(0) if match else None


def test_articles_with_times_title_link_and_source(client, overview):
    html = client.get("/articles").text

    assert overview["source"] is None
    assert text_of(html, "article-count") == "2 Artikel."
    assert text_of(html, "title-11") == "Meldung &lt;11&gt;"
    title = tag_of(html, "title-11")
    assert 'href="https://example.org/11.html"' in title and 'rel="noopener noreferrer"' in title
    assert '<a href="/sources/2">EZB</a>' in html
    assert '<time datetime="2026-10-23T09:30:00+00:00" data-local-time>' in html


def test_deleted_source_and_missing_values(client, overview):
    html = client.get("/articles").text

    # No title: the link shows the URL. No date: a dash, never a guess.
    assert text_of(html, "title-10") == "https://example.org/10.html"
    assert text_of(html, "published-10") == "–"
    assert text_of(html, "source-10") == "Alt (gelöscht)"


def test_filter_is_passed_on_and_selected(client, overview):
    overview["overview"] = ArticleOverview(source_id=3, sources=OVERVIEW.sources, articles=[], matching=0)

    html = client.get("/articles?source=3").text

    assert overview["source"] == "3"
    assert '<option value="3" selected>Fed</option>' in html
    assert text_of(html, "no-articles") == "Noch keine Artikel dieser Quelle. Aktive Quellen ruft der Worker in ihrem Abrufintervall ab."


def test_more_articles_than_shown(client, overview):
    overview["overview"] = ArticleOverview(source_id=None, sources=[], articles=[article(1)], matching=250)

    assert text_of(client.get("/articles").text, "article-count") == "Angezeigt: die zuletzt abgerufenen 1 von 250 Artikeln."


def test_navigation_marks_the_article_page(client, overview):
    assert 'aria-current="page"' in tag_of(client.get("/articles").text, "nav-articles")
