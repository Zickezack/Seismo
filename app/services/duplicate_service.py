"""Identical articles: the same canonical URL or the same cleaned text (userstories.md, Normalisierung,
Deduplizierung und Clustering, story 2).

A duplicate is kept - every source that found it stays traceable - and points to its original: the article published
first, on equal or unknown times the one stored first (an unknown time never comes before a known one, datamodel.md,
rule 5). An article found later but published earlier becomes the original, and the group follows it. The order is
settled when an article joins its group; a time that changes afterwards does not reorder it. Later steps work on
originals only.

Both checks run in the transaction that stores what they look at, under one advisory lock, so two workers never take
the same original.
"""
import hashlib
from collections.abc import Iterable
from urllib.parse import urlsplit

from sqlalchemy import Connection, Row

from app.db.models import DuplicateReason, FullTextStatus
from app.repositories import article_repository

# A shorter text says too little to make two articles one: a heading, a cookie notice, a bot check.
MIN_TEXT_FOR_HASH = 200


def content_hash(text: str | None) -> str | None:
    """The fingerprint of a cleaned text, from MIN_TEXT_FOR_HASH characters on; None for a shorter one."""
    if not text or len(text) < MIN_TEXT_FOR_HASH:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def lock(connection: Connection) -> None:
    """The checks' lock, until the end of the transaction. A transaction that writes existing articles before its
    check takes it first: otherwise it could hold an article another check waits for while it waits for that check."""
    article_repository.lock_duplicate_check(connection)


def mark_new(connection: Connection, article_ids: Iterable[int]) -> list[int]:
    """At ingestion, before any full text: a new article with the link of a stored one is its duplicate.

    Returns the ids whose full text is to be fetched: the originals. A duplicate's full text is skipped - the page is
    the original's.
    """
    article_ids = sorted(article_ids)
    if not article_ids:
        return []
    lock(connection)
    originals = []
    for article_id in article_ids:
        article = article_repository.get_for_duplicates(connection, article_id)
        found = article_repository.originals_matching(connection, article_id, canonical_url=article.canonical_url)
        if not found or _join(connection, article, found[0], DuplicateReason.URL) == article_id:
            originals.append(article_id)
    return originals


def check(connection: Connection, article_id: int) -> None:
    """After normalization, in the transaction that writes it: the page's own canonical link may only now match
    another article, and its text may be the same as one on another host.

    The same text on the same host under another canonical URL never counts: by the publisher's own word these are
    different pages, so the text is no report (an error page, a consent wall).
    """
    lock(connection)
    article = article_repository.get_for_duplicates(connection, article_id)
    if article.duplicate_of_id is not None:
        return
    if found := article_repository.originals_matching(connection, article_id, canonical_url=article.canonical_url):
        _join(connection, article, found[0], DuplicateReason.URL)
        return
    if not article.content_hash:
        return
    host = _host(article.canonical_url)
    found = article_repository.originals_matching(connection, article_id, content_hash=article.content_hash)
    if other_host := [row for row in found if _host(row.match_url) != host]:
        _join(connection, article, other_host[0], DuplicateReason.TEXT)


def _join(connection: Connection, article: Row, original: Row, reason: DuplicateReason) -> int:
    """The article joins the original's group, or leads it when it comes first. Returns the group's original."""
    first, then = sorted((article, original), key=_order)
    article_repository.mark_duplicate(connection, then.id, first.id, reason)
    if reason == DuplicateReason.URL:
        # Only while it waits: a full text stored before stays.
        article_repository.finish_full_text(connection, then.id, FullTextStatus.SKIPPED, None)
    return first.id


def _order(row: Row) -> tuple:
    """Published first, an unknown time after every known one, then stored first."""
    return row.published_at is None, row.published_at, row.id


def _host(url: str | None) -> str | None:
    return urlsplit(url or "").hostname
