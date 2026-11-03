"""The web layer's pages ask the database for the navigation's unread alerts and a page's watchlists; the unit tests
run without one, so both are replaced unless a test replaces them itself."""
import pytest

from app.services import alert_service, watchlist_service


@pytest.fixture(autouse=True)
def no_alerts(monkeypatch):
    monkeypatch.setattr(alert_service, "unread_count", lambda _engine: 0)
    monkeypatch.setattr(watchlist_service, "watching", lambda _engine, _kind, _target: watchlist_service.Watching([], []))
