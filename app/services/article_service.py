"""Artikel as the analyst sees them: the most recently fetched, all or of one source."""
from dataclasses import dataclass, fields
from datetime import datetime

from sqlalchemy import Engine

from app.db.models import FullTextStatus
from app.repositories import article_repository, source_repository
from app.services import source_service

LIST_LIMIT = 100
FULL_TEXT_LABELS = {
    FullTextStatus.PENDING: "Ausstehend",
    FullTextStatus.STORED: "Gespeichert",
    FullTextStatus.DISALLOWED: "Gesperrt",
    FullTextStatus.UNSUPPORTED: "Nicht unterstützt",
    FullTextStatus.FAILED: "Fehlgeschlagen",
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

    @property
    def full_text_label(self) -> str:
        return FULL_TEXT_LABELS[self.full_text_status]

    @property
    def full_text_size(self) -> str:
        # A no-break space: number and unit stay on one line in a narrow cell.
        return f"{max(1, round(self.full_text_bytes / 1024))}\u00a0KB" if self.full_text_bytes else ""

    @property
    def language_label(self) -> str:
        return source_service.LANGUAGES.get(self.language, self.language) if self.language else "–"


@dataclass(frozen=True)
class SourceChoice:
    id: int
    name: str


@dataclass(frozen=True)
class ArticleOverview:
    source_id: int | None  # the active filter
    sources: list[SourceChoice]  # what the filter offers
    articles: list[ArticleListItem]  # the newest LIST_LIMIT matching the filter
    matching: int


def overview(engine: Engine, source: str | None) -> ArticleOverview:
    with engine.connect() as connection:
        sources = [SourceChoice(row.id, row.name) for row in source_repository.all_by_name(connection)]
        # Anything but the id of an existing source means no filter.
        source_id = next((choice.id for choice in sources if str(choice.id) == (source or "").strip()), None)
        rows = article_repository.latest(connection, source_id, LIST_LIMIT)
        matching = article_repository.count(connection, source_id)
    return ArticleOverview(
        source_id=source_id,
        sources=sources,
        articles=[ArticleListItem(**{field.name: row._mapping[field.name] for field in fields(ArticleListItem)}) for row in rows],
        matching=matching,
    )


def count_by_source(engine: Engine) -> dict[int, int]:
    with engine.connect() as connection:
        return article_repository.count_by_source(connection)
