"""Artikel as the analyst sees them: the most recently fetched, all or of one source; one article with its duplicates
and similar articles, and the admin's corrections of them."""
from collections.abc import Iterable
from dataclasses import dataclass, fields
from datetime import datetime

from sqlalchemy import Engine, Row

from app.db.models import BACKFILL_PRIORITY, LIVE_PRIORITY, FullTextStatus
from app.repositories import article_link_repository, article_repository, source_repository
from app.services import duplicate_service, ingestion_service, source_service
from app.services.bulk import BulkResult

LIST_LIMIT = 100
FULL_TEXT_LABELS = {
    FullTextStatus.PENDING: "Ausstehend",
    FullTextStatus.STORED: "Gespeichert",
    FullTextStatus.DISALLOWED: "Gesperrt",
    FullTextStatus.UNSUPPORTED: "Nicht unterstützt",
    FullTextStatus.FAILED: "Fehlgeschlagen",
    FullTextStatus.BLOCKED: "Blockiert",
    FullTextStatus.RESERVED: "Vorbehalten",
    FullTextStatus.SKIPPED: "Übersprungen",
}


@dataclass(frozen=True)
class ArticleListItem:
    id: int
    url: str
    title: str | None
    published_at: datetime | None
    fetched_at: datetime
    language: str | None
    source_id: int | None
    source_display_name: str  # the source's current name, or the copy kept after it was deleted
    source_exists: bool
    full_text_status: str
    full_text_error: str | None
    full_text_bytes: int | None  # size of the raw text while it is kept
    detected_language: str | None
    published_at_source: str | None
    normalization_status: str
    normalization_error: str | None
    text_chars: int | None  # length of the cleaned text while the raw text is kept
    canonical_url: str | None
    duplicate_of_id: int | None
    duplicate_reason: str | None
    original_source_name: str | None  # the original's source: its current name, or the copy kept after deletion
    duplicate_count: int  # an original's
    link_count: int  # similar articles, separated ones not counted
    first_linked_source: str | None  # the source of a linked article published before this one; None: this one first

    @property
    def full_text_label(self) -> str:
        return FULL_TEXT_LABELS[self.full_text_status]

    @property
    def full_text_size(self) -> str:
        # A no-break space: number and unit stay on one line in a narrow cell.
        return f"{max(1, round(self.full_text_bytes / 1024))}\u00a0KB" if self.full_text_bytes else ""

    @property
    def language_label(self) -> str:
        """The language recognised in the text, else the one the source states."""
        language = self.detected_language or self.language
        return source_service.LANGUAGES.get(language, language) if language else "–"

    @property
    def published_note(self) -> str:
        """Where the publication time came from, when not simply from the feed."""
        return {"page": "laut Seite", "gdelt": "von GDELT gesehen"}.get(self.published_at_source or "", "")

    @property
    def text_note(self) -> str:
        """What normalization made of the raw text."""
        if self.normalization_status == "failed":
            return "Text nicht lesbar"
        if self.text_chars is None:
            return ""
        return f"Text {self.text_chars}\u00a0Zeichen" if self.text_chars else "kein Text erkannt"

    @property
    def duplicate_note(self) -> str:
        """Why it is a duplicate, and of which source's article."""
        if self.duplicate_of_id is None:
            return ""
        return f"{DUPLICATE_LABELS.get(self.duplicate_reason, 'Duplikat')} von {self.original_source_name}"

    @property
    def relations_note(self) -> str:
        """An original's duplicates and similar articles, and which came first."""
        parts = []
        if self.duplicate_count:
            parts.append(f"{self.duplicate_count} {'Duplikat' if self.duplicate_count == 1 else 'Duplikate'}")
        if self.link_count:
            parts.append(f"ähnlich wie {self.link_count} Artikel, zuerst: {self.first_linked_source or 'dieser'}")
        note = " · ".join(parts)
        return note[:1].upper() + note[1:]


DUPLICATE_LABELS = {"url": "Duplikat (gleiche URL)", "text": "Duplikat (gleicher Text)"}


@dataclass(frozen=True)
class RelatedArticle:
    """A duplicate, an original or a similar article as the article page lists it."""

    id: int
    title: str | None
    url: str
    published_at: datetime | None
    source_display_name: str
    duplicate_reason: str | None = None
    similarity: float | None = None

    @property
    def duplicate_label(self) -> str:
        return DUPLICATE_LABELS.get(self.duplicate_reason or "", "")

    @property
    def similarity_label(self) -> str:
        return f"{round(self.similarity * 100)}\u00a0%" if self.similarity is not None else ""


@dataclass(frozen=True)
class ArticleDetail:
    article: ArticleListItem
    original: RelatedArticle | None  # of a duplicate
    duplicates: list[RelatedArticle]  # of an original, first published first
    similar: list[RelatedArticle]  # of an original, first published first
    first_published_id: int  # among the article and its similar ones


@dataclass(frozen=True)
class SourceChoice:
    id: int
    name: str


@dataclass(frozen=True)
class ArticleOverview:
    source_id: int | None  # the active filter
    hide_duplicates: bool  # the other one
    sources: list[SourceChoice]  # what the filter offers
    articles: list[ArticleListItem]  # the newest LIST_LIMIT matching the filter
    matching: int


def overview(engine: Engine, source: str | None, hide_duplicates: bool = False) -> ArticleOverview:
    with engine.connect() as connection:
        sources = [SourceChoice(row.id, row.name) for row in source_repository.all_by_name(connection)]
        # Anything but the id of an existing source means no filter.
        source_id = next((choice.id for choice in sources if str(choice.id) == (source or "").strip()), None)
        rows = article_repository.latest(connection, source_id, LIST_LIMIT, hide_duplicates)
        matching = article_repository.count(connection, source_id, hide_duplicates)
    return ArticleOverview(
        source_id=source_id,
        hide_duplicates=hide_duplicates,
        sources=sources,
        articles=[_list_item(row) for row in rows],
        matching=matching,
    )


def count_by_source(engine: Engine) -> dict[int, int]:
    with engine.connect() as connection:
        return article_repository.count_by_source(connection)


def blocked_by_source(engine: Engine) -> dict[int, int]:
    """Articles per source whose full text the host refused (Blockiert)."""
    with engine.connect() as connection:
        return article_repository.count_by_source(connection, FullTextStatus.BLOCKED)


def detail(engine: Engine, article_id: int) -> ArticleDetail | None:
    """The article page: the article, a duplicate's original, an original's duplicates and similar articles."""
    with engine.connect() as connection:
        row = article_repository.listed(connection, article_id)
        if row is None:
            return None
        article = _list_item(row)
        original = None
        if article.duplicate_of_id is not None:
            [original] = map(_related, article_repository.summaries(connection, article_id=article.duplicate_of_id))
        duplicates = [_related(each) for each in article_repository.summaries(connection, duplicates_of=article_id)]
        similar = [_related(each) for each in article_link_repository.linked(connection, article_id)]
    # The order of a group (duplicate_service): published first, an unknown time after every known one, then stored first.
    first = min([article, *similar], key=lambda each: (each.published_at is None, each.published_at, each.id))
    return ArticleDetail(article, original, duplicates, similar, first.id)


def not_duplicate(engine: Engine, article_ids: Iterable[int]) -> BulkResult:
    """The admin's word: these duplicates are no copies of their originals (duplicate_service.separate). A full text
    skipped as the original's page is fetched now. All or nothing (requirements.md, "Destructive and Bulk Actions")."""
    article_ids = sorted(set(article_ids))
    if not article_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        # The duplicate checks' lock before any article's row, as the checks themselves take them.
        duplicate_service.lock(connection)
        found = {row.id: row for row in article_repository.lock_duplicates(connection, article_ids)}
        blocked = [
            f"Artikel #{article_id} ({'nicht gefunden' if article_id not in found else 'kein Duplikat'})"
            for article_id in article_ids
            if article_id not in found or found[article_id].duplicate_of_id is None
        ]
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        for row in found.values():
            duplicate_service.separate(connection, row.id, row.duplicate_of_id)
            if row.full_text_status == FullTextStatus.SKIPPED:
                priority = BACKFILL_PRIORITY if row.backfill_id is not None else LIVE_PRIORITY
                ingestion_service.enqueue_full_texts(connection, [row.id], priority)
    return BulkResult(processed=len(article_ids), blocked=[])


def _list_item(row: Row) -> ArticleListItem:
    return ArticleListItem(**{field.name: row._mapping[field.name] for field in fields(ArticleListItem)})


def _related(row: Row) -> RelatedArticle:
    names = [field.name for field in fields(RelatedArticle) if field.name in row._mapping]
    return RelatedArticle(**{name: row._mapping[name] for name in names})
