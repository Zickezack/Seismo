"""Artikel page in the web layer, without a database: the service is replaced."""
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import AuthSettings, Settings
from app.services import article_service, similarity_service
from app.services.article_service import ArticleListItem, ArticleOverview, SourceChoice
from app.services.bulk import BulkResult
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
        canonical_url=None, duplicate_of_id=None, duplicate_reason=None, original_source_name=None, duplicate_count=0,
        link_count=0, first_linked_source=None,
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


def test_a_duplicate_and_an_original_s_relations_link_to_the_article_page(client, overview):
    overview["overview"] = ArticleOverview(
        source_id=None,
        hide_duplicates=False,
        sources=[],
        articles=[
            article(5, duplicate_of_id=4, duplicate_reason="url", original_source_name="Agentur"),
            article(6, duplicate_of_id=4, duplicate_reason="text", original_source_name="Agentur"),
            article(4, duplicate_count=2, link_count=3, first_linked_source="Presse"),
            article(7, link_count=1),
            article(8),
        ],
        matching=5,
    )

    html = client.get("/articles").text

    assert hint_of(html, "duplicate-5") == ("/articles/5", "Duplikat (gleiche URL) von Agentur")
    assert hint_of(html, "duplicate-6") == ("/articles/6", "Duplikat (gleicher Text) von Agentur")
    assert hint_of(html, "relations-4") == ("/articles/4", "2 Duplikate · ähnlich wie 3 Artikel, zuerst: Presse")
    assert hint_of(html, "relations-7") == ("/articles/7", "Ähnlich wie 1 Artikel, zuerst: dieser")
    assert tag_of(html, "duplicate-4") is None and tag_of(html, "relations-8") is None


def hint_of(html, testid):
    match = re.search(rf'data-testid="{testid}"><a href="([^"]+)">([^<]*)</a>', html)
    return match.groups() if match else None


def test_duplicates_can_be_hidden_and_the_count_says_so(client, overview):
    overview["overview"] = ArticleOverview(source_id=None, hide_duplicates=True, sources=[], articles=[article(1)], matching=1)

    html = client.get("/articles?source=&hide_duplicates=1").text

    assert overview["hide_duplicates"] is True
    assert "checked" in tag_of(html, "hide-duplicates")
    assert text_of(html, "article-count") == "1 Artikel ohne Duplikate."
    assert client.get("/articles") and overview["hide_duplicates"] is False


def related(article_id, **fields):
    values = dict(id=article_id, title=f"Meldung {article_id}", url=f"https://example.org/{article_id}.html", published_at=NOW,
                  source_display_name="Agentur")
    return article_service.RelatedArticle(**values | fields)


def detail(article_value, **fields):
    values = dict(article=article_value, original=None, duplicates=[], similar=[], first_published_id=article_value.id)
    return article_service.ArticleDetail(**values | fields)


@pytest.fixture
def page(monkeypatch):
    calls = {}
    monkeypatch.setattr(article_service, "detail", lambda _engine, article_id: calls.get("detail"))
    return calls


def test_an_original_s_page_lists_its_duplicates_and_similar_articles_first_published_marked(client, page):
    page["detail"] = detail(
        article(4, canonical_url="https://example.org/4.html", duplicate_count=1, link_count=2),
        duplicates=[related(5, duplicate_reason="url")],
        similar=[related(6, similarity=0.72), related(7, similarity=0.9)],
        first_published_id=6,
    )

    html = client.get("/articles/4").text

    assert text_of(html, "article-title") == "Meldung &lt;4&gt;"
    assert text_of(html, "meta-canonical") == "https://example.org/4.html"
    assert 'name="article_id" value="5"' in tag_of(html, "select-5")
    assert tag_of(html, "duplicate-list").startswith('<form class="stack" method="post" action="/articles/4/not-duplicate"')
    not_duplicate = tag_of(html, "not-duplicate")
    assert 'data-confirm="Duplikat|Duplikate"' in not_duplicate and 'data-confirm-verb="trennen"' in not_duplicate
    assert tag_of(html, "similar-list").startswith('<form class="stack" method="post" action="/articles/4/separate"')
    assert 'data-confirm-verb="aufheben"' in tag_of(html, "separate")
    assert (text_of(html, "similarity-6"), text_of(html, "similarity-7")) == ("72 %", "90 %")
    assert text_of(html, "first-6") == "zuerst veröffentlicht" and tag_of(html, "first-7") is None
    assert text_of(html, "related-title-6") == "Meldung 6" and 'href="/articles/6"' in tag_of(html, "related-title-6")


def test_a_duplicate_s_page_offers_to_separate_it_from_its_original(client, page):
    page["detail"] = detail(
        article(5, duplicate_of_id=4, duplicate_reason="text", original_source_name="Agentur"),
        original=related(4, title="Das Original"),
    )

    html = client.get("/articles/5").text

    # The one row is the original; its checkbox sends this article, named in the confirmation.
    checkbox = tag_of(html, "select-4")
    assert 'value="5"' in checkbox and 'data-name="Meldung &lt;5&gt;"' in checkbox
    assert tag_of(html, "original-list").startswith('<form class="stack" method="post" action="/articles/5/not-duplicate"')
    assert tag_of(html, "similar-section") is None


def test_an_article_without_relations_says_so_and_an_unknown_one_goes_back_to_the_list(client, page, overview):
    page["detail"] = detail(article(8))
    assert text_of(client.get("/articles/8").text, "no-similar") == "Keine ähnlichen Artikel."

    page["detail"] = None
    response = client.get("/articles/99")
    assert (response.status_code, response.headers["location"]) == (303, "/articles")
    assert text_of(client.get("/articles").text, "flash") == "Artikel nicht gefunden."


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (BulkResult(processed=2, blocked=[]), "2 Verknüpfungen aufgehoben."),
        (BulkResult(processed=1, blocked=[]), "1 Verknüpfung aufgehoben."),
        (BulkResult(processed=0, blocked=["Artikel #7 (nicht verknüpft)"]), "Nichts aufgehoben, blockiert: Artikel #7 (nicht verknüpft)."),
        (BulkResult(processed=0, blocked=[]), "Keine Artikel ausgewählt."),
    ],
)
def test_separating_similar_articles_passes_the_selection_and_reports_the_result(client, monkeypatch, overview, result, message):
    calls = []
    monkeypatch.setattr(similarity_service, "separate", lambda _engine, article_id, ids: calls.append((article_id, ids)) or result)

    response = client.post("/articles/4/separate", data={"article_id": ["6", "7"]})

    assert calls == [(4, [6, 7])]
    assert response.headers["location"] == "/articles/4#similar"
    assert text_of(client.get("/articles").text, "flash") == message


def test_not_duplicate_passes_the_selection_and_reports_the_result(client, monkeypatch, overview):
    calls = []
    monkeypatch.setattr(article_service, "not_duplicate", lambda _engine, ids: calls.append(ids) or BulkResult(processed=1, blocked=[]))

    response = client.post("/articles/5/not-duplicate", data={"article_id": ["5"]})

    assert calls == [[5]]
    assert response.headers["location"] == "/articles/5"
    assert text_of(client.get("/articles").text, "flash") == "1 Duplikat getrennt."
