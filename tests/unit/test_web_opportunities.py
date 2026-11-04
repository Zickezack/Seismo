"""The page Chancen and the start page's section, without a database."""
import re
from datetime import UTC, datetime, timedelta

import pytest

from app.services import opportunity_service
from app.services.event_study_service import Move, Status
from app.services.impact_service import Likelihood
from app.services.opportunity_service import Criteria, Kind, Opportunity, Overview
from tests.unit.test_web_articles import tag_of, text_of
from tests.unit.test_web_dashboard import board  # noqa: F401
from tests.unit.test_web_events import client  # noqa: F401
from tests.unit.test_web_impacts import hypothesis

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


def inner(html, testid, end="</td>"):
    match = re.search(rf'data-testid="{testid}"[^>]*>(.*?){end}', html, re.DOTALL)
    return " ".join(re.sub(r"<[^>]+>", " ", match.group(1)).split()) if match else None


def chance(id=1, kind=Kind.QUIET, t=-0.58):
    view = hypothesis(id, likelihood=Likelihood(probability="medium"))
    move = Move(9, "SHEL", "XLE", Status.DONE if t is not None else None, None if t is None else -0.0415, t, 3, True)
    return Opportunity(7, "Freight rates squeeze oil majors", "start", NOW - timedelta(days=2), NOW, False, view, move, kind)


@pytest.fixture
def chances(monkeypatch):
    calls = {}

    def overview(_engine, period="", criteria=Criteria()):
        calls["period"], calls["criteria"] = period, criteria
        return calls.get("overview") or Overview(
            "7d", NOW - timedelta(days=7), criteria, [chance(), chance(2, Kind.NO_CLOSE, None)], 2,
            {Kind.QUIET: 1, Kind.NO_CLOSE: 1, Kind.MOVED: 3, Kind.NO_PRICE: 40}, 120,
        )

    monkeypatch.setattr(opportunity_service, "overview", overview)
    return calls


def test_the_page_lists_the_chances_side_by_side_and_counts_what_it_leaves_out(client, chances):
    html = client.get("/opportunities").text

    assert tag_of(html, "nav-opportunities") is not None
    assert text_of(html, "opportunity-counts") == "2 Chancen aus 120 Ereignissen. 3 schon in Richtung bewegt, 40 ohne US-Wertpapier mit Kursen."
    assert inner(html, "opportunity-move-1") == "SHEL gegen XLE: kaum bewegt -4,15 % abnormal, t -0,58, 3 Handelstage"
    assert inner(html, "opportunity-move-2") == "SHEL gegen XLE: noch kein Schluss seit dem Ereignis"
    assert inner(html, "opportunity-says-1").startswith("negativ · Monate · plausibel")
    assert text_of(html, "opportunity-likelihood-1") == "" and "<span>mittel</span>" in html
    assert 'aria-current="true"' in tag_of(html, "period-7d")


def test_period_filter_and_order_come_from_the_url_and_stay_in_the_links(client, chances):
    html = client.get("/opportunities", params={"period": "30d", "direction": "negative", "horizon": "x", "sort": "move"}).text

    assert chances["period"] == "30d" and chances["criteria"] == Criteria(direction="negative", sort="move")
    assert '<option value="move" selected>' in html
    assert 'href="/opportunities?direction=negative&amp;sort=move&amp;period=24h"' in html


def test_nothing_found_says_so(client, chances):
    chances["overview"] = Overview("24h", NOW, Criteria(probability="high"), [], 0, {}, 3)
    assert text_of(client.get("/opportunities").text, "no-opportunities") == "Keine Chance in den letzten 24 Stunden, die zur Auswahl passt."


def test_the_start_page_shows_the_newest_chances_of_its_period(client, board, monkeypatch):
    asked = {}

    def opportunities(_engine, since, *_args, limit=200, **_kwargs):
        asked["since"], asked["limit"] = since, limit
        return [chance()], 12, {}, 30

    monkeypatch.setattr(opportunity_service, "opportunities", opportunities)
    html = client.get("/").text

    assert asked == {"since": board["dashboard"].since, "limit": 5}
    assert inner(html, "opportunity-move-1") == "SHEL gegen XLE: kaum bewegt -4,15 % abnormal, t -0,58, 3 Handelstage"
    assert tag_of(html, "chances-all") is not None and "Angezeigt: 1 von 12" in inner(html, "chances-more", "</p>")
