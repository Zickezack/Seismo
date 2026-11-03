"""Alerts, alert rules and watchlists in the web layer, without a database: the services are replaced."""
import re

import pytest
from fastapi.testclient import TestClient

from app.services import alert_service, event_service, watchlist_service
from app.services.alert_service import AlertEvent, AlertView, Inbox, ReportHit, RuleView
from app.services.bulk import BulkResult
from app.services.watchlist_service import WatchItemView, WatchlistDetail, WatchlistListItem, WatchlistRef
from app.web.main import create_app
from tests.unit.test_web_articles import AUTH, NOW, SETTINGS, tag_of, text_of
from tests.unit.test_web_events import CHOICES


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(event_service, "filter_choices", lambda _engine: CHOICES)
    with TestClient(create_app(SETTINGS, AUTH), follow_redirects=False) as client:
        client.post("/login", data={"username": "admin", "password": "correct horse battery"})
        yield client


def rule(rule_id=4, **fields):
    values = dict(
        id=rule_id, name="Häfen", is_active=True, watchlist_id=2, watchlist_name="Europa", criteria={"type": "labor"},
        on_new=True, on_change=True, on_report=False, max_per_hour=5, cooldown_hours=6, active_since=NOW, waiting_count=3,
        alert_count=7, last_alert_at=NOW,
    )
    return RuleView(**values | fields)


def test_the_navigation_counts_the_unread_alerts(client, monkeypatch):
    monkeypatch.setattr(alert_service, "unread_count", lambda _engine: 3)
    monkeypatch.setattr(alert_service, "rules", lambda _engine: [])

    html = client.get("/alerts/rules").text

    assert text_of(html, "unread-alerts") == "3"


def test_the_inbox_says_what_happened_to_each_event(client, monkeypatch):
    single = AlertView(1, 4, "Häfen", False, NOW, None, [
        AlertEvent(10, "Strike <Rotterdam>", None, new=True, changes=["Eskalation"],
                   reports=[ReportHit("Report", 30, "NOS"), ReportHit("Other", 31, None)]),
    ])
    digest = AlertView(2, 4, "Häfen", True, NOW, NOW, [AlertEvent(11, "A", None, new=True), AlertEvent(12, "B", 20, new=True)])
    asked = {}

    def inbox(_engine, unread_only):
        asked["unread_only"] = unread_only
        return Inbox([single, digest], 2, 1, unread_only)

    monkeypatch.setattr(alert_service, "inbox", inbox)

    html = client.get("/alerts", params={"show": "all"}).text

    assert asked["unread_only"] is False
    assert text_of(html, "summary-1") == "Neues Ereignis, Zustand: Eskalation, 2 neue Berichte"
    assert "Strike &lt;Rotterdam&gt;" in html
    assert text_of(html, "unread-1") == "ungelesen" and text_of(html, "unread-2") is None
    assert text_of(html, "digest-2") == "Sammel-Alert"
    # A merged event leads to the one it went into.
    assert re.search(r'data-testid="alert-2-event-12">\s*<a href="/events/20">B</a>', html)
    assert 'aria-current="page"' in tag_of(html, "nav-alerts")


def test_marking_read_keeps_the_view_and_names_what_blocked(client, monkeypatch):
    monkeypatch.setattr(alert_service, "mark_read", lambda _engine, ids: BulkResult(0, [f"Alert #{ids[0]} (nicht gefunden)"]))

    monkeypatch.setattr(alert_service, "inbox", lambda _engine, unread_only: Inbox([], 0, 0, unread_only))

    response = client.post("/alerts/read", data={"alert_id": "9", "back": "all"})

    assert response.headers["location"] == "/alerts?show=all"
    assert "Nichts als gelesen markiert, blockiert: Alert #9 (nicht gefunden)." in client.get(response.headers["location"]).text


def test_the_rule_list_shows_watchlist_triggers_throttle_and_waiting_hits(client, monkeypatch):
    monkeypatch.setattr(alert_service, "rules", lambda _engine: [rule(), rule(5, name="Alles", is_active=False, watchlist_id=None, criteria={}, cooldown_hours=0, waiting_count=0)])

    html = client.get("/alerts/rules").text

    assert text_of(html, "name-4") == "Häfen"
    assert tag_of(html, "events-4") == '<a href="/events?type=labor&amp;watchlist=2" data-testid="events-4">'
    assert "Neues Ereignis, Zustandswechsel" in html
    assert "höchstens 5 pro Stunde, ein Ereignis erneut nach 6 h" in html and "ohne Ruhezeit" in html
    assert text_of(html, "waiting-4") == "3 Treffer warten" and text_of(html, "waiting-5") is None


def test_a_new_rule_takes_the_filter_of_the_event_list(client):
    html = client.get("/alerts/rules/new", params={"type": "labor", "country": "CH", "period": "3d"}).text

    assert "selected" in re.search(r'<option value="labor"[^>]*>', html).group(0)
    assert "selected" in re.search(r'<option value="CH"[^>]*>', html).group(0)
    assert 'value="5"' in tag_of(html, "rule-max-per-hour") and 'value="6"' in tag_of(html, "rule-cooldown")
    assert "checked" in tag_of(html, "rule-on-new") and "checked" not in tag_of(html, "rule-on-report")


def test_an_invalid_rule_is_not_saved_and_says_why(client):
    response = client.post("/alerts/rules", data={"name": "x", "max_per_hour": "5", "cooldown_hours": "6"})

    assert response.headers["location"] == "/alerts/rules/new"
    assert "Nicht angelegt: Melden bei: bitte mindestens eines wählen." in client.get("/alerts/rules/new").text


def test_the_event_filter_offers_to_save_itself_as_a_rule(client, monkeypatch):
    monkeypatch.setattr(event_service, "overview", lambda _engine, _criteria: event_service.EventOverview([], 0, 0))

    plain = client.get("/events").text
    filtered = client.get("/events", params={"type": "labor", "period": "7d"}).text

    assert tag_of(plain, "save-as-rule") is None
    assert tag_of(filtered, "save-as-rule") == '<a class="button button-secondary" href="/alerts/rules/new?type=labor" data-testid="save-as-rule">'


def test_the_watchlist_page_lists_what_it_follows_and_renames_in_place(client, monkeypatch):
    detail = WatchlistDetail(WatchlistListItem(2, "Europa", 2, 1, NOW), [
        WatchItemView(5, "entity", "Adif", "/entities/9", "Unternehmen"), WatchItemView(6, "country", "ES", None),
    ])
    monkeypatch.setattr(watchlist_service, "detail", lambda _engine, _id: detail)
    renamed = {}
    monkeypatch.setattr(watchlist_service, "rename", lambda _engine, watchlist_id, name: renamed.update(name=name) or WatchlistRef(watchlist_id, name))

    html = client.get("/watchlists/2").text
    answer = client.post("/watchlists/2/rename", data={"name": "Südeuropa"}, headers={"Accept": "application/json"})

    assert tag_of(html, "item-label-5") == '<a href="/entities/9" data-testid="item-label-5">'
    assert text_of(html, "item-label-6") == "ES"
    assert tag_of(html, "watchlist-events") == '<a href="/events?watchlist=2" data-testid="watchlist-events">'
    assert answer.json() == {"status": "Gespeichert"} and renamed["name"] == "Südeuropa"


def test_an_invalid_name_answers_in_place_with_its_error(client, monkeypatch):
    def rename(_engine, _id, _name):
        raise watchlist_service.InvalidWatchlist("Name: bitte angeben.")

    monkeypatch.setattr(watchlist_service, "rename", rename)

    answer = client.post("/watchlists/2/rename", data={"name": ""}, headers={"Accept": "application/json"})

    assert answer.status_code == 200 and answer.json() == {"error": "Name: bitte angeben."}


@pytest.mark.parametrize(("back", "location"), [("/entities/9", "/entities/9"), ("//evil.example/x", "/watchlists"), ("https://evil.example", "/watchlists")])
def test_following_goes_back_to_the_page_it_came_from_on_this_site_only(client, monkeypatch, back, location):
    added = {}
    monkeypatch.setattr(watchlist_service, "add", lambda _engine, *args: added.update(args=args) or ("Europa", True))

    response = client.post("/watchlists/follow", data={"watchlist": "2", "kind": "entity", "target": "9", "back": back})

    assert response.headers["location"] == location
    assert added["args"] == (2, "entity", "9")


def test_a_watchlist_a_rule_uses_is_not_deleted(client, monkeypatch):
    monkeypatch.setattr(watchlist_service, "delete", lambda _engine, _ids: BulkResult(0, ["Europa (von der Alert-Regel Häfen verwendet)"]))
    monkeypatch.setattr(watchlist_service, "overview", lambda _engine: [])

    client.post("/watchlists/delete", data={"watchlist_id": "2"})

    assert "Nichts gelöscht, blockiert: Europa (von der Alert-Regel Häfen verwendet)." in client.get("/watchlists").text
