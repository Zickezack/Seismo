"""Backfill rules without a database: the period from the form, where the next page ends, what the list shows."""
from datetime import UTC, date, datetime, timedelta

import pytest

from app.db.models import BackfillStatus
from app.services import backfill_service
from app.services.backfill_service import Backfill, InvalidBackfill, next_cursor, parse_period
from app.sources.base import FetchedItem

NOW = datetime(2026, 10, 23, 15, 30, tzinfo=UTC)
GDELT_REACH = timedelta(days=90)


def period(first, last, reach=GDELT_REACH):
    return parse_period({"period_start": first, "period_end": last}, reach, NOW)


def test_period_is_whole_days_in_utc_from_von_to_the_end_of_bis():
    assert period("2026-09-01", "2026-09-30") == (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))


def test_a_period_up_to_today_ends_now():
    assert period("2026-10-23", "2026-10-23") == (datetime(2026, 10, 23, tzinfo=UTC), NOW)


@pytest.mark.parametrize(
    ("first", "last", "message"),
    [
        ("", "2026-10-01", "Von: bitte ein Datum wählen."),
        ("2026-10-01", "1.10.2026", "Bis: bitte ein Datum wählen."),
        ("2026-10-01", "2026-10-24", "Bis: höchstens heute."),
        ("2026-10-02", "2026-10-01", "Von: darf nicht nach Bis liegen."),
        # 90 days back from 23.10. is 25.07.; the first whole day within reach is 26.07.
        ("2026-07-25", "2026-08-01", "Von: frühestens 26.07.2026, weiter reicht diese Quelle nicht zurück."),
    ],
)
def test_invalid_period_is_named(first, last, message):
    with pytest.raises(InvalidBackfill) as error:
        period(first, last)

    assert str(error.value) == message


def test_a_feed_has_no_reach_of_its_own():
    assert period("2020-01-01", "2020-01-31", reach=None)[0] == datetime(2020, 1, 1, tzinfo=UTC)
    assert period("2026-07-26", "2026-08-01")[0] == datetime(2026, 7, 26, tzinfo=UTC)


def item(published_at):
    return FetchedItem(key=str(published_at), url="https://bank.example/a", title=None, published_at=published_at, language=None)


CURSOR = datetime(2026, 9, 30, tzinfo=UTC)
START = datetime(2026, 9, 1, tzinfo=UTC)


def test_a_page_that_is_not_full_ends_the_backfill():
    assert next_cursor(250, [item(CURSOR - timedelta(hours=1))] * 249, CURSOR, START) == (None, False)
    # A feed: one request returns all there is.
    assert next_cursor(None, [item(CURSOR)] * 500, CURSOR, START) == (None, False)


def test_a_full_page_continues_one_second_past_its_oldest_item():
    oldest = datetime(2026, 9, 20, 6, 15, tzinfo=UTC)
    items = [item(oldest + timedelta(minutes=15 * index)) for index in range(3)]

    assert next_cursor(3, items, CURSOR, START) == (oldest + timedelta(seconds=1), False)


def test_a_full_page_within_one_moment_steps_back_and_says_so():
    moment = CURSOR - timedelta(seconds=1)

    assert next_cursor(2, [item(moment), item(moment)], CURSOR, START) == (CURSOR - timedelta(minutes=15), True)
    assert next_cursor(2, [item(None), item(None)], CURSOR, START) == (CURSOR - timedelta(minutes=15), True)


def test_the_backfill_ends_once_the_next_page_would_start_before_its_period():
    assert next_cursor(1, [item(START - timedelta(hours=1))], CURSOR, START) == (None, False)


def test_list_item_shows_the_days_of_its_period_and_its_status():
    backfill = Backfill(
        id=1, source_id=2, period_start=datetime(2026, 9, 1, tzinfo=UTC), period_end=datetime(2026, 10, 1, tzinfo=UTC),
        cursor=datetime(2026, 9, 15, tzinfo=UTC), status=BackfillStatus.RUNNING, articles_found=300, articles_new=280,
        last_error=None, created_at=NOW, finished_at=None, full_texts_stored=100, full_texts_pending=180,
    )

    assert (backfill.first_day, backfill.last_day) == (date(2026, 9, 1), date(2026, 9, 30))
    assert (backfill.status_label, backfill.is_running) == ("Läuft", True)
    assert {status: label for status, label in backfill_service.STATUS_LABELS.items()} == {
        "running": "Läuft", "done": "Abgeschlossen", "failed": "Fehlgeschlagen", "cancelled": "Abgebrochen",
    }
