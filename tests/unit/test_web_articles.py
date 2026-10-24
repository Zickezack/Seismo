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
        full_text_status="stored", full_text_error=None, full_text_bytes=48_000, detected_language=None,
        published_at_source="feed", normalization_status="pending", normalization_error=None, text_chars=None,
        duplicate_of_id=None, duplicate_reason=None, original_url=None, original_source_name=None,
    )
    return ArticleListItem(**values | fields)


OVERVIEW = ArticleOverview(
    source_id=None,
    hide_duplicates=False,
    sources=[SourceChoice(2, "EZB"), SourceChoice(3, "Fed")],
    articles=[
        article(11),
        article(
            10, title=None, published_at=None, language=None, source_id=None, source_display_name="Alt",
            source_exists=False, full_text_status="disallowed", full_text_error="Durch robots.txt gesperrt",
            full_text_bytes=None,
        ),
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

    def fake(_engine, source, hide_duplicates):
        calls["source"], calls["hide_duplicates"] = source, hide_duplicates
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
    overview["overview"] = ArticleOverview(source_id=3, hide_duplicates=False, sources=OVERVIEW.sources, articles=[], matching=0)

    html = client.get("/articles?source=3").text

    assert overview["source"] == "3"
    assert '<option value="3" selected>Fed</option>' in html
    assert text_of(html, "no-articles") == "Noch keine Artikel dieser Quelle. Aktive Quellen ruft der Worker in ihrem Abrufintervall ab."


def test_more_articles_than_shown(client, overview):
    overview["overview"] = ArticleOverview(source_id=None, hide_duplicates=False, sources=[], articles=[article(1)], matching=250)

    assert text_of(client.get("/articles").text, "article-count") == "Angezeigt: die zuletzt abgerufenen 1 von 250 Artikeln."


def test_navigation_marks_the_article_page(client, overview):
    assert 'aria-current="page"' in tag_of(client.get("/articles").text, "nav-articles")


def test_full_text_status_size_and_reason(client, overview):
    html = client.get("/articles").text

    assert '<span class="status status-stored">Gespeichert</span> 47\u00a0KB' in html
    assert '<span class="status status-disallowed">Gesperrt</span>' in html
    assert text_of(html, "full-text-error-10") == "Durch robots.txt gesperrt"
    assert tag_of(html, "full-text-error-11") is None


def test_list_shows_the_detected_language_where_the_time_came_from_and_the_text_length(client, monkeypatch):
    overview = ArticleOverview(
        source_id=None,
        hide_duplicates=False,
        sources=[],
        articles=[
            article(1, language="en", detected_language="de", published_at_source="page", normalization_status="done", text_chars=4512),
            article(2, published_at_source="gdelt", normalization_status="done", text_chars=0),
            article(3, normalization_status="failed", normalization_error="PdfReadError: EOF marker not found"),
            article(4),
        ],
        matching=4,
    )
    monkeypatch.setattr(article_service, "overview", lambda _engine, _source, _hide: overview)

    html = client.get("/articles").text

    assert '<td>Deutsch</td>' in re.search(r'data-testid="article-1".*?</tr>', html, re.DOTALL).group(0)
    assert text_of(html, "published-note-1") == "laut Seite"
    assert text_of(html, "published-note-2") == "von GDELT gesehen"
    assert tag_of(html, "published-note-4") is None
    assert text_of(html, "text-note-1") == "Text 4512\u00a0Zeichen"
    assert text_of(html, "text-note-2") == "kein Text erkannt"
    assert text_of(html, "text-note-3") == "Text nicht lesbar"
    assert 'title="PdfReadError: EOF marker not found"' in tag_of(html, "text-note-3")
    assert tag_of(html, "text-note-4") is None


def test_a_duplicate_names_why_and_links_its_original_s_source(client, overview):
    overview["overview"] = ArticleOverview(
        source_id=None,
        hide_duplicates=False,
        sources=[],
        articles=[
            article(5, duplicate_of_id=4, duplicate_reason="url", original_url="https://agency.example/4", original_source_name="Agentur"),
            article(6, duplicate_of_id=4, duplicate_reason="text", original_url="https://agency.example/4", original_source_name="Agentur"),
            article(4),
        ],
        matching=3,
    )

    html = client.get("/articles").text

    assert text_of(html, "duplicate-5") == "Duplikat (gleiche URL) von"
    hint = re.search(r'data-testid="duplicate-5">(.*?)</span>', html).group(1)
    assert '<a href="https://agency.example/4" target="_blank" rel="noopener noreferrer">Agentur</a>' in hint
    assert text_of(html, "duplicate-6") == "Duplikat (gleicher Text) von"
    assert tag_of(html, "duplicate-4") is None


def test_duplicates_can_be_hidden_and_the_count_says_so(client, overview):
    overview["overview"] = ArticleOverview(source_id=None, hide_duplicates=True, sources=[], articles=[article(1)], matching=1)

    html = client.get("/articles?source=&hide_duplicates=1").text

    assert overview["hide_duplicates"] is True
    assert "checked" in tag_of(html, "hide-duplicates")
    assert text_of(html, "article-count") == "1 Artikel ohne Duplikate."
    assert client.get("/articles") and overview["hide_duplicates"] is False
