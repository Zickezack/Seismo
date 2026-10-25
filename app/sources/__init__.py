"""Source adapters: each fetches one type of source and returns its items in one shape (base.FetchedItem).

A new source type: an adapter module here and its entry in ADAPTERS; articles and everything after
them stay the same (userstories.md, Quellenadapter und Ingestion, story 2).
"""
from app.sources.base import SourceAdapter
from app.sources.eia import EiaAdapter
from app.sources.gdelt import GdeltAdapter
from app.sources.reliefweb import ReliefWebAdapter
from app.sources.rss import RssAdapter

ADAPTERS: dict[str, SourceAdapter] = {
    "rss": RssAdapter(),
    "gdelt": GdeltAdapter(),
    "reliefweb": ReliefWebAdapter(),
    "eia": EiaAdapter(),
}
ADAPTER_LABELS = {adapter_type: adapter.label for adapter_type, adapter in ADAPTERS.items()}
