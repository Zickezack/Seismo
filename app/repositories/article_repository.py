"""Data access for the Artikel."""
from collections.abc import Sequence

from sqlalchemy import Connection, Row, Select, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from app.db.models import Article, DuplicateReason, FullTextStatus, RawText, Source

# Of the two-key advisory lock that serialises the duplicate check (llm_slot_repository uses 7301).
DUPLICATE_LOCK_NAMESPACE = 7302


def insert_new(connection: Connection, rows: list[dict]) -> list[int]:
    """Insert the rows whose (source_id, item_key) is not stored yet; the ids of the inserted ones."""
    if not rows:
        return []
    statement = (
        insert(Article)
        .values(rows)
        .on_conflict_do_nothing(index_elements=[Article.source_id, Article.item_key])
        .returning(Article.id)
    )
    return sorted(connection.execute(statement).scalars())


def get_for_full_text(connection: Connection, article_id: int) -> Row | None:
    """URL and full-text status of the article, whether its source still exists and is active, and the source's
    own minimum interval between requests."""
    statement = (
        select(
            Article.id,
            Article.url,
            Article.canonical_url,
            Article.full_text_status,
            Source.is_active.label("source_active"),
            Source.min_request_interval_seconds.label("source_min_request_interval_seconds"),
        )
        .outerjoin(Source, Source.id == Article.source_id)
        .where(Article.id == article_id)
    )
    return connection.execute(statement).one_or_none()


def finish_full_text(connection: Connection, article_id: int, status: FullTextStatus, error: str | None) -> bool:
    """Record the outcome, once: False when the article is gone or its full text was settled before."""
    statement = (
        update(Article)
        .where(Article.id == article_id, Article.full_text_status == FullTextStatus.PENDING)
        .values(full_text_status=status, full_text_error=error)
    )
    return connection.execute(statement).rowcount == 1


def lock_duplicate_check(connection: Connection) -> None:
    """Until the end of the transaction no other one checks for duplicates, so two never take the same original."""
    statement = text("SELECT pg_advisory_xact_lock(CAST(:namespace AS integer), 0)")
    connection.execute(statement, {"namespace": DUPLICATE_LOCK_NAMESPACE})


def get_for_duplicates(connection: Connection, article_id: int) -> Row | None:
    statement = select(
        Article.id, Article.canonical_url, Article.content_hash, Article.published_at, Article.duplicate_of_id
    ).where(Article.id == article_id)
    return connection.execute(statement).one_or_none()


def originals_matching(
    connection: Connection, article_id: int, *, canonical_url: str | None = None, content_hash: str | None = None
) -> Sequence[Row]:
    """The originals of the other articles with this canonical URL or this text, first published first: the original's
    id and time, and the matching article's canonical URL (whose host the text is on)."""
    match = aliased(Article)
    original = aliased(Article)
    original_id = func.coalesce(match.duplicate_of_id, match.id)
    if canonical_url:
        condition = match.canonical_url == canonical_url
    elif content_hash:
        condition = match.content_hash == content_hash
    else:
        return []
    statement = (
        select(original.id, original.published_at, match.canonical_url.label("match_url"))
        .select_from(match)
        .join(original, original.id == original_id)
        # Neither the article itself nor its own duplicates.
        .where(condition, match.id != article_id, original_id != article_id)
        .order_by(original.published_at.asc().nulls_last(), original.id)
    )
    return connection.execute(statement).all()


def mark_duplicate(connection: Connection, article_id: int, original_id: int, reason: DuplicateReason) -> None:
    """The article becomes a duplicate of the original, and so do its own duplicates: none points to another one."""
    connection.execute(
        update(Article).where(Article.id == article_id).values(duplicate_of_id=original_id, duplicate_reason=reason)
    )
    connection.execute(update(Article).where(Article.duplicate_of_id == article_id).values(duplicate_of_id=original_id))


def latest(connection: Connection, source_id: int | None, limit: int, hide_duplicates: bool = False) -> Sequence[Row]:
    """The most recently fetched articles, with the source's current name while it exists, and a duplicate's original:
    its link and its source's name."""
    original = aliased(Article)
    original_source = aliased(Source)
    statement = (
        select(
            Article.__table__,
            func.coalesce(Source.name, Article.source_name).label("source_display_name"),
            (Source.id.is_not(None)).label("source_exists"),
            func.octet_length(RawText.content).label("full_text_bytes"),
            func.length(RawText.plain_text).label("text_chars"),
            original.url.label("original_url"),
            func.coalesce(original_source.name, original.source_name).label("original_source_name"),
        )
        .outerjoin(Source, Source.id == Article.source_id)
        .outerjoin(RawText, RawText.article_id == Article.id)
        .outerjoin(original, original.id == Article.duplicate_of_id)
        .outerjoin(original_source, original_source.id == original.source_id)
        .order_by(Article.fetched_at.desc(), Article.published_at.desc().nulls_last(), Article.id.desc())
        .limit(limit)
    )
    return connection.execute(_filtered(statement, source_id, hide_duplicates)).all()


def count(connection: Connection, source_id: int | None, hide_duplicates: bool = False) -> int:
    statement = select(func.count()).select_from(Article)
    return connection.execute(_filtered(statement, source_id, hide_duplicates)).scalar_one()


def _filtered(statement: Select, source_id: int | None, hide_duplicates: bool) -> Select:
    if source_id is not None:
        statement = statement.where(Article.source_id == source_id)
    if hide_duplicates:
        statement = statement.where(Article.duplicate_of_id.is_(None))
    return statement


def count_by_source(connection: Connection, full_text_status: FullTextStatus | None = None) -> dict[int, int]:
    """Articles per existing source, all of them or those with this full-text status."""
    statement = select(Article.source_id, func.count()).where(Article.source_id.is_not(None)).group_by(Article.source_id)
    if full_text_status is not None:
        statement = statement.where(Article.full_text_status == full_text_status)
    return dict(connection.execute(statement).all())


def reset_blocked(connection: Connection, source_ids: list[int]) -> Sequence[Row]:
    """The blocked full texts of these sources back to pending; id and backfill of each article."""
    statement = (
        update(Article)
        .where(Article.source_id.in_(source_ids), Article.full_text_status == FullTextStatus.BLOCKED)
        .values(full_text_status=FullTextStatus.PENDING, full_text_error=None)
        .returning(Article.id, Article.backfill_id)
    )
    return connection.execute(statement).all()
