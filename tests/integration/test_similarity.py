"""Similar articles in PostgreSQL: linked at normalization from 60 % within 7 days, never identical text on one host,
none for a duplicate, carried along when an original becomes a duplicate; the admin's corrections - separating a link,
"Kein Duplikat" - hold for good."""
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.db.models import Article, ArticleLink
from app.repositories import article_repository
from app.services import article_service, duplicate_service, job_service, similarity_service
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_ingestion import NOW, articles, jobs
from tests.integration.test_normalization import article, normalize_all, source
from tests.unit.test_normalization_service import page

WORDS = [f"Begriff{number}" for number in range(240)]


def story(changed=0):
    """A report of 240 words in sentences of 12; `changed` words, spread over it, replaced by others."""
    words = list(WORDS)
    for position in range(changed):
        index = (position * 37) % len(words)
        words[index] = f"Anders{position}"
    sentences = [" ".join(words[start : start + 12]) + "." for start in range(0, len(words), 12)]
    return page(body="<article>" + "".join(f"<p>{sentence}</p>" for sentence in sentences) + "</article>")


def links(database):
    with database.connect() as connection:
        return [tuple(row) for row in connection.execute(
            select(ArticleLink.article_id, ArticleLink.other_id, ArticleLink.separated_at.is_not(None)).order_by(ArticleLink.article_id)
        )]


def add(database, source_id, key, raw, host, **fields):
    return article(database, source_id, key, raw, url=f"https://{host}/{key}.html", **fields)


@pytest.fixture
def source_id(database):
    return source(database)


def test_a_lightly_edited_copy_on_another_host_is_linked_and_both_stay_originals(database, source_id):
    a = add(database, source_id, "a", story(), "a.example")
    b = add(database, source_id, "b", story(changed=4), "b.example")

    normalize_all(database)

    assert links(database) == [(a, b, False)]
    with database.connect() as connection:
        similarity = connection.execute(select(ArticleLink.similarity)).scalar_one()
    assert 0.6 <= similarity < 1
    assert [row.duplicate_of_id for row in articles(database)] == [None, None]


@pytest.mark.parametrize(
    ("changed", "apart", "host"),
    [(60, timedelta(0), "b.example"), (4, timedelta(days=8), "b.example")],
    ids=["rewritten", "eight days apart"],
)
def test_a_rewritten_text_or_one_published_more_than_a_week_apart_is_not_linked(database, source_id, changed, apart, host):
    add(database, source_id, "a", story(), "a.example")
    add(database, source_id, "b", story(changed=changed), host, published_at=NOW - apart)

    normalize_all(database)

    assert links(database) == []


def test_identical_text_on_one_host_under_another_canonical_url_is_neither_duplicate_nor_linked(database, source_id):
    add(database, source_id, "a", story(), "q.example")
    add(database, source_id, "b", story(), "q.example")

    normalize_all(database)

    assert links(database) == []
    assert [row.duplicate_of_id for row in articles(database)] == [None, None]


def test_an_original_that_becomes_a_duplicate_hands_its_links_to_the_new_original(database, source_id):
    x = add(database, source_id, "x", story(), "x.example", canonical_url="https://x.example/x.html")
    y = add(database, source_id, "y", story(changed=4), "y.example")
    normalize_all(database)
    assert links(database) == [(x, y, False)]
    # The same page found again, published an hour earlier: the new original.
    z = article(database, source_id, "z", url="https://x.example/x.html", canonical_url="https://x.example/x.html", published_at=NOW - timedelta(hours=1))
    with database.begin() as connection:
        duplicate_service.mark_new(connection, [z])

    assert links(database) == [(y, z, False)]
    detail = article_service.detail(database, y)
    assert [related.id for related in detail.similar] == [z]
    assert detail.first_published_id == z


def test_a_separated_link_is_never_made_again(database, source_id):
    a = add(database, source_id, "a", story(), "a.example")
    b = add(database, source_id, "b", story(changed=4), "b.example")
    normalize_all(database)

    result = similarity_service.separate(database, a, [b])

    assert (result.processed, result.blocked) == (1, [])
    renormalize(database)
    assert links(database) == [(a, b, True)]
    assert article_service.detail(database, a).similar == []
    blocked = similarity_service.separate(database, a, [b])
    assert (blocked.processed, blocked.blocked) == (0, [f"Artikel #{b} (nicht verknüpft)"])


def test_not_duplicate_keeps_the_pair_apart_for_good_and_fetches_a_skipped_full_text(database, source_id):
    original = add(database, source_id, "o", story(), "a.example", canonical_url="https://a.example/o.html")
    copy = add(database, source_id, "c", story(), "b.example")
    # Found again by another feed: stored as new, its full text still to come.
    with database.begin() as connection:
        [same_page] = article_repository.insert_new(connection, [{
            "source_id": source_id, "source_name": "Q", "source_url": "https://q.example/rss", "item_key": "p",
            "url": "https://a.example/o.html?utm_source=rss", "canonical_url": "https://a.example/o.html", "title": "Meldung p",
            # Clearly after the original: NOW of test_ingestion and of test_normalization may be a second apart.
            "published_at": NOW + timedelta(hours=1), "language": "en",
        }])
        duplicate_service.mark_new(connection, [same_page])
    normalize_all(database)
    rows = {row.id: row for row in articles(database)}
    assert (rows[copy].duplicate_reason, rows[same_page].duplicate_reason, rows[same_page].full_text_status) == ("text", "url", "skipped")

    result = article_service.not_duplicate(database, [copy, same_page])

    assert (result.processed, result.blocked) == (2, [])
    rows = {row.id: row for row in articles(database)}
    assert [rows[key].duplicate_of_id for key in (copy, same_page)] == [None, None]
    assert rows[same_page].full_text_status == "pending"
    assert same_page in [job.payload["article_id"] for job in jobs(database, job_service.FETCH_ARTICLE_JOB_KIND)]
    # Normalized again, the copy finds its old original - separated - and links to it neither.
    normalize_all(database)
    rows = {row.id: row for row in articles(database)}
    assert rows[copy].duplicate_of_id is None
    assert (original, copy, True) in links(database)


def test_not_duplicate_is_all_or_nothing(database, source_id):
    original = add(database, source_id, "o", story(), "a.example")
    copy = add(database, source_id, "c", story(), "b.example")
    normalize_all(database)

    result = article_service.not_duplicate(database, [copy, original, 999])

    assert (result.processed, result.blocked) == (0, [f"Artikel #{original} (kein Duplikat)", "Artikel #999 (nicht gefunden)"])
    assert {row.id: row.duplicate_of_id for row in articles(database)}[copy] == original


def renormalize(database):
    with database.begin() as connection:
        connection.execute(update(Article).values(normalization_status="pending", normalized_at=None))
    normalize_all(database)
