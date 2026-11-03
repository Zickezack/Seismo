"""Watchlists and alert rules in PostgreSQL (userstories.md, Dashboard, Recherche und Alerts, story 3): what a rule
finds, each once, and how its throttle lets it go out."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.models import Alert, AlertHit, AlertRule, Entity, EntityAlias, Event, Instrument
from app.services import alert_service, event_correction_service, event_service, watchlist_service
from tests.integration.test_dashboard import FLOOD, HOURS, STRIKE
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_event_filter import named
from tests.integration.test_events import QUAKE, RATES, SAME_QUAKE, assign_all, assigned, reported
from tests.integration.test_extraction import Ollama

AWAY = Ollama(status=503)
WILDFIRE = {"title": "Wildfire spreads north of Athens", "place": "Varnavas", "actors": [], "event_type": "natural_disaster",
            "summary": "A wildfire burned forest and homes north of Athens.", "country": "GR"}


def now():
    # The sweep compares with the database's clock: what happened since the rule is active, within its look-back.
    return datetime.now(UTC)


def rule(database, active_since=None, **form):
    """A rule of the form's defaults with `form` in place, active since `active_since` (a day ago by default)."""
    values = {"name": "Regel", "is_active": "1", "on_new": "1", "on_change": "1", "max_per_hour": "5", "cooldown_hours": "6"}
    created = alert_service.create(database, values | form)
    with database.begin() as connection:
        connection.execute(update(AlertRule).where(AlertRule.id == created.id).values(active_since=active_since or now() - 24 * HOURS))
    return created.id


def watchlist(database, name="Beobachtet", **items):
    """A watchlist following `items`: kind -> list of targets."""
    created = watchlist_service.create(database, name)
    for kind, targets in items.items():
        for target in targets:
            watchlist_service.add(database, created.id, kind, target)
    return created.id


def sent(database):
    """Each alert, the earliest first: whether a Sammel-Alert, and its hits as (event, trigger)."""
    with database.connect() as connection:
        alerts = connection.execute(select(Alert.id, Alert.digest).order_by(Alert.id)).all()
        hits = connection.execute(select(AlertHit.alert_id, AlertHit.event_id, AlertHit.trigger).order_by(AlertHit.id)).all()
    return [(each.digest, sorted((hit.event_id, hit.trigger) for hit in hits if hit.alert_id == each.id)) for each in alerts]


def waiting(database):
    with database.connect() as connection:
        return connection.execute(select(func.count()).where(AlertHit.alert_id.is_(None))).scalar_one()


def event_of(database, candidate_id):
    return assigned(database)[candidate_id].event_id


def age_alerts(database, by):
    """Every alert sent `by` earlier, as if that time had passed."""
    with database.begin() as connection:
        connection.execute(update(Alert).values(created_at=Alert.created_at - by))


def test_an_event_a_watched_entity_is_named_in_alerts_once(database):
    [flood] = reported(database, "a", FLOOD, published_at=now() - 2 * HOURS)
    [strike] = reported(database, "b", STRIKE, published_at=now() - HOURS)
    adif = named(database, flood, "company", "Adif")
    assign_all(database, AWAY)
    rule(database, watchlist=str(watchlist(database, entity=[adif])))

    assert alert_service.sweep(database) == 1
    assert sent(database) == [(False, [(event_of(database, flood), "new")])]
    # Found once: a second sweep finds nothing again.
    assert alert_service.sweep(database) == 0
    assert alert_service.unread_count(database) == 1
    assert event_of(database, strike) not in {hit[0] for _, hits in sent(database) for hit in hits}


def test_what_happened_before_the_rule_was_active_does_not_count(database):
    reported(database, "a", FLOOD, published_at=now() - 30 * HOURS)
    assign_all(database, AWAY)
    rule(database, active_since=now() - 24 * HOURS)

    assert alert_service.sweep(database) == 0
    assert waiting(database) == 0


def test_a_change_of_state_alerts_even_within_the_quiet_period_and_a_report_waits_for_its_end(database):
    [quake] = reported(database, "a", QUAKE | {"status": "start"}, published_at=now() - 3 * HOURS)
    assign_all(database, AWAY)
    rule(database, on_report="1", cooldown_hours="6")
    assert alert_service.sweep(database) == 1
    event_id = event_of(database, quake)

    reported(database, "b", SAME_QUAKE | {"status": "escalation"}, published_at=now() - 2 * HOURS)
    assign_all(database, AWAY)
    assert alert_service.sweep(database) == 1
    assert sent(database)[1] == (False, [(event_id, "change"), (event_id, "report")])

    reported(database, "c", SAME_QUAKE | {"status": "update"}, published_at=now() - HOURS)
    assign_all(database, AWAY)
    assert alert_service.sweep(database) == 0
    assert waiting(database) == 1
    # Once the quiet period is over, it goes out.
    age_alerts(database, 6 * HOURS)
    assert alert_service.sweep(database) == 1
    assert sent(database)[2] == (False, [(event_id, "report")])


def test_beyond_the_hourly_limit_the_rest_goes_out_as_one_digest_once_the_hour_allows(database):
    candidates = [
        reported(database, key, report, published_at=now() - HOURS)[0]
        for key, report in (("a", FLOOD), ("b", STRIKE), ("c", RATES), ("d", QUAKE))
    ]
    assign_all(database, AWAY)
    rule(database, max_per_hour="2")

    assert alert_service.sweep(database) == 2
    first, digest = sent(database)
    assert first == (False, [(event_of(database, candidates[0]), "new")])
    assert digest == (True, sorted((event_of(database, each), "new") for each in candidates[1:]))

    [later] = reported(database, "e", WILDFIRE, published_at=now() - HOURS)
    assign_all(database, AWAY)
    assert alert_service.sweep(database) == 0
    assert waiting(database) == 1
    age_alerts(database, HOURS)
    assert alert_service.sweep(database) == 1
    assert sent(database)[2] == (False, [(event_of(database, later), "new")])


def test_an_event_reaching_the_relevance_asked_for_later_is_new_then(database):
    [quake] = reported(database, "a", QUAKE, published_at=now() - 3 * HOURS)
    assign_all(database, AWAY)
    rule(database, sources="2")
    assert alert_service.sweep(database) == 0

    reported(database, "b", SAME_QUAKE | {"status": "update"}, published_at=now() - HOURS)
    assign_all(database, AWAY)
    assert alert_service.sweep(database) == 1
    assert sent(database) == [(False, [(event_of(database, quake), "new")])]


def test_a_watchlist_follows_types_countries_events_and_the_entity_of_a_wertpapier(database):
    [flood] = reported(database, "a", FLOOD, published_at=now() - 4 * HOURS)
    [strike] = reported(database, "b", STRIKE, published_at=now() - 3 * HOURS)
    [rates] = reported(database, "c", RATES, published_at=now() - 2 * HOURS)
    [quake] = reported(database, "d", QUAKE, published_at=now() - HOURS)
    ecb = named(database, rates, "authority", "European Central Bank", "ECB")
    assign_all(database, AWAY)
    with database.begin() as connection:
        instrument_id = connection.execute(Instrument.__table__.insert().values(
            source="six", source_ref="X", symbol="X", exchange="SIX", name="X", kind="index", entity_id=ecb,
        ).returning(Instrument.id)).scalar_one()
    events = {name: event_of(database, each) for name, each in (("flood", flood), ("strike", strike), ("rates", rates), ("quake", quake))}

    def matching(**items):
        watched = watchlist(database, f"W{len(items)}{sorted(items)}", **items)
        form = event_service.parse_filter({"watchlist": str(watched)})
        return {name for name, event_id in events.items() if event_id in {each.id for each in event_service.overview(database, form.criteria).events}}

    assert matching(event_type=["labor"]) == {"strike"}
    assert matching(country=["ES"]) == {"flood"}
    assert matching(event=[str(events["quake"])]) == {"quake"}
    assert matching(instrument=[str(instrument_id)]) == {"rates"}
    assert matching(country=["ES"], event_type=["labor"]) == {"flood", "strike"}
    assert matching() == set()


def test_a_merged_entity_and_a_merged_event_stand_for_the_one_they_went_into(database):
    [flood] = reported(database, "a", FLOOD, published_at=now() - 4 * HOURS)
    [quake] = reported(database, "b", QUAKE, published_at=now() - 3 * HOURS)
    [strike] = reported(database, "c", STRIKE, published_at=now() - 2 * HOURS)
    adif = named(database, flood, "company", "Adif")
    assign_all(database, AWAY)
    other = named(database, strike, "company", "Adif SA")
    with database.begin() as connection:
        # As a merge leaves them: the name forms go to the one that remains, the merged one points there.
        connection.execute(update(EntityAlias).where(EntityAlias.entity_id == adif).values(entity_id=other))
        connection.execute(update(Entity).where(Entity.id == adif).values(merged_into_id=other))
        connection.execute(update(Event).where(Event.id == event_of(database, quake)).values(merged_into_id=event_of(database, strike)))

    def listed(watched):
        form = event_service.parse_filter({"watchlist": str(watched)})
        return {each.id for each in event_service.overview(database, form.criteria).events}

    # Both name the entity that remains; the quake's reports are in the strike now.
    assert listed(watchlist(database, "Adif", entity=[adif])) == {event_of(database, flood), event_of(database, strike)}
    assert listed(watchlist(database, "Beben", event=[str(event_of(database, quake))])) == {event_of(database, strike)}


def test_an_inactive_rule_finds_nothing_and_counts_from_its_activation_on(database):
    rule_id = rule(database, is_active="")
    reported(database, "a", FLOOD, published_at=now() - HOURS)
    assign_all(database, AWAY)
    assert alert_service.sweep(database) == 0

    alert_service.set_active(database, [rule_id], True)
    assert alert_service.sweep(database) == 0
    assert waiting(database) == 0


def test_an_event_split_off_by_the_analyst_is_no_new_event(database):
    [quake] = reported(database, "a", QUAKE, published_at=now() - 3 * HOURS)
    [same] = reported(database, "b", SAME_QUAKE | {"status": "update"}, published_at=now() - 2 * HOURS)
    assign_all(database, AWAY)
    rule(database)
    assert alert_service.sweep(database) == 1

    event_correction_service.split(database, event_of(database, quake), [same])

    assert alert_service.sweep(database) == 0


def test_alerts_are_read_selected_or_all(database):
    for key, report in (("a", FLOOD), ("b", STRIKE)):
        reported(database, key, report, published_at=now() - HOURS)
    assign_all(database, AWAY)
    rule(database)
    alert_service.sweep(database)
    inbox = alert_service.inbox(database, unread_only=True)
    assert (inbox.total, inbox.unread) == (2, 2)
    assert [alert.summary for alert in inbox.alerts] == ["Neues Ereignis", "Neues Ereignis"]

    result = alert_service.mark_read(database, [inbox.alerts[0].id, 999])
    assert (result.processed, result.blocked) == (0, ["Alert #999 (nicht gefunden)"])
    assert alert_service.mark_read(database, [inbox.alerts[0].id]).processed == 1
    assert alert_service.unread_count(database) == 1
    assert alert_service.mark_all_read(database) == 1
    assert alert_service.inbox(database, unread_only=True).alerts == []
    assert len(alert_service.inbox(database, unread_only=False).alerts) == 2


def test_a_watchlist_a_rule_uses_is_not_deleted_and_names_the_rule(database):
    used = watchlist(database, "Häfen")
    free = watchlist(database, "Banken")
    rule(database, name="Hafen-Alerts", watchlist=str(used))

    result = watchlist_service.delete(database, [used, free])

    assert (result.processed, result.blocked) == (0, ["Häfen (von der Alert-Regel Hafen-Alerts verwendet)"])
    assert watchlist_service.delete(database, [free]).processed == 1


def test_names_are_unique_regardless_of_case(database):
    watchlist(database, "Häfen")
    rule(database, name="Hafen")
    with pytest.raises(watchlist_service.InvalidWatchlist, match="bereits vergeben"):
        watchlist_service.create(database, "HÄFEN")
    with pytest.raises(alert_service.InvalidRule, match="bereits vergeben"):
        rule(database, name="hafen")


def test_deleting_a_rule_takes_its_hits_and_alerts(database):
    reported(database, "a", FLOOD, published_at=now() - HOURS)
    assign_all(database, AWAY)
    rule_id = rule(database)
    alert_service.sweep(database)

    assert alert_service.delete(database, [rule_id]).processed == 1
    assert sent(database) == []


def test_a_watchlist_follows_only_what_exists_each_once_and_removes_all_or_nothing(database):
    [flood] = reported(database, "a", FLOOD, published_at=now() - HOURS)
    adif = named(database, flood, "company", "Adif")
    watched = watchlist(database)

    assert watchlist_service.add(database, watched, "entity", str(adif)) == ("Beobachtet", True)
    assert watchlist_service.add(database, watched, "entity", str(adif)) == ("Beobachtet", False)
    assert watchlist_service.add(database, watched, "country", " es ") == ("Beobachtet", True)
    for kind, target, message in (("entity", "999", "Entität nicht gefunden."), ("event", "x", "Ereignis nicht gefunden."),
                                  ("instrument", "1", "Wertpapier nicht gefunden."), ("event_type", "war", "Ereignistyp"),
                                  ("country", "ESP", "Land")):
        with pytest.raises(watchlist_service.InvalidWatchlist, match=message):
            watchlist_service.add(database, watched, kind, target)
    items = watchlist_service.detail(database, watched).items
    assert [(item.kind, item.label) for item in items] == [("entity", "Adif"), ("country", "ES")]

    result = watchlist_service.remove_items(database, watched, [items[0].id, 999])
    assert (result.processed, result.blocked) == (0, ["Eintrag #999 (nicht gefunden)"])
    assert watchlist_service.remove_items(database, watched, [items[0].id]).processed == 1
    assert [item.label for item in watchlist_service.detail(database, watched).items] == ["ES"]
