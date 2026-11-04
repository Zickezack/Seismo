"""Chancen in PostgreSQL (userstories.md, Dashboard, Recherche und Alerts, story 5): the Wirkungsannahmen of active
events whose Wertpapier has not moved much since the Ereignistag - or against them -, measured from synthetic prices."""
import random
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import insert, select

from app.db.models import EventState, Instrument, PricePoint
from app.services import opportunity_service
from app.services.opportunity_service import Criteria, Kind
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_forecasts import DROP, SAYS, SHELL, TODAY, TOTAL, instrument, oil  # noqa: F401
from tests.integration.test_impacts import answer, assess, hypothesis

# Two weeks after the event: Shell has fallen against the oil ETF day by day since, TotalEnergies moved with it.
LATER = datetime.combine(TODAY + timedelta(days=14), time(23), UTC)
SINCE = LATER - timedelta(days=30)


def with_total(database, oil):
    """TotalEnergies's Wertpapier: the oil ETF's prices with a little noise of its own."""
    rng = random.Random(3)
    with database.begin() as connection:
        total_id = instrument(connection, "TTE", entity_id=oil["total"])
        rows = connection.execute(select(PricePoint.__table__).where(PricePoint.instrument_id == oil["ids"]["XLE"])).all()
        copies = []
        for row in rows:
            values = {key: value for key, value in row._mapping.items() if key != "id"}
            close = float(row.close) * (1 + rng.gauss(0, 0.002))
            copies.append(values | {"instrument_id": total_id, "open": close, "high": close, "low": close, "close": round(close, 6)})
        connection.execute(insert(PricePoint), copies)
    return total_id


def found(database, criteria=Criteria()):
    chances, total, counts, events = opportunity_service.opportunities(database, SINCE, criteria, LATER)
    return {each.hypothesis.target_name: each for each in chances}, counts


def test_a_wertpapier_within_its_noise_or_moved_against_the_assumption_is_a_chance(database, oil):
    with_total(database, oil)
    assess(database, oil["event_id"], answer(
        hypothesis(SHELL, direction="positive") | SAYS,  # Shell fell: against it
        hypothesis(TOTAL) | SAYS,  # TotalEnergies moved with its sector: within its noise
    ))

    chances, counts = found(database)

    assert {name: each.kind for name, each in chances.items()} == {"Shell": Kind.AGAINST, "TotalEnergies": Kind.QUIET}
    shell = chances["Shell"]
    assert (shell.move.symbol, shell.move.benchmark_symbol) == ("SHEL", "XLE") and shell.move.t < -1
    assert shell.move.abnormal < 0 and shell.move.days >= 9
    assert abs(chances["TotalEnergies"].move.t) < 1
    assert counts == {Kind.AGAINST: 1, Kind.QUIET: 1}
    assert shell.new and shell.hypothesis.likelihood.probability == "high" and shell.hypothesis.support


def test_one_moved_in_its_direction_already_is_only_counted(database, oil):
    assess(database, oil["event_id"], answer(hypothesis(SHELL) | SAYS, hypothesis(TOTAL) | SAYS))

    chances, counts = found(database)

    assert chances == {}
    assert counts == {Kind.MOVED: 1, Kind.NO_PRICE: 1}  # TotalEnergies without a Wertpapier here
    view = opportunity_service.overview(database, "30d", now=LATER)
    assert view.left_out == [("schon in Richtung bewegt", 1), ("ohne US-Wertpapier mit Kursen", 1)]


def test_before_a_close_since_the_event_nothing_has_moved_and_criteria_filter(database, oil):
    assess(database, oil["event_id"], answer(hypothesis(SHELL) | SAYS))
    reported = opportunity_service.opportunities(database, datetime.now(UTC) - timedelta(days=1))[0]
    # The Ereignistag closes after the first report, today at the earliest: no close since yet, or only that one.
    assert [each.kind for each in reported] in ([Kind.NO_CLOSE], [Kind.QUIET], [Kind.AGAINST], [])

    assert found(database, Criteria(direction="positive"))[1] == {}
    assert found(database, Criteria(horizon="months"))[1] == {Kind.MOVED: 1}


def test_a_closed_event_offers_no_chance(database, oil):
    with_total(database, oil)
    assess(database, oil["event_id"], answer(hypothesis(TOTAL) | SAYS))
    with database.begin() as connection:
        connection.execute(insert(EventState).values(
            event_id=oil["event_id"], status="closure", valid_from=datetime.now(UTC), recorded_at=datetime.now(UTC),
        ))
    assert found(database) == ({}, {})
