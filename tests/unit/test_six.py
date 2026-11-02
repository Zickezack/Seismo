"""SIX's prices without the network: the request, and what is made of the answer (app/prices/six.py)."""
import json
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from app.prices import six
from app.sources.errors import SourceRejected
from app.sources.http import HttpResponse

FIELDS = ["ValorId", "ClosingPrice", "PreviousClosingPrice", "OpeningPrice", "DailyHighPrice", "DailyLowPrice", "TotalVolume",
          "LatestTradeDate", "LatestTradeTime"]


class FakeHttp:
    def __init__(self, body):
        self.body, self.requests = body, []

    def get(self, url, accept, permitted=False, headers=None):
        self.requests.append((url, accept, permitted, headers))
        return HttpResponse(url=url, content=json.dumps(self.body).encode() if not isinstance(self.body, bytes) else self.body)


def test_one_request_asks_for_every_valor_id_and_robots_txt_is_kept():
    http = FakeHttp({"colNames": FIELDS, "rowData": [
        ["CH0038863350CHF4", 79.5, 79.1, 79.2, 79.9, 78.8, 3200000, 20261030, 173644.267],
        ["CH0009980894CHF9", 12001.5, 11950, None, None, None, None, 20261030, 173000],
    ]})

    quotes, missing = six.latest(http, ["CH0038863350CHF4", "CH0009980894CHF9", "CH0000000000CHF4"])

    [(url, accept, permitted, _headers)] = http.requests
    parts = urlsplit(url)
    assert (parts.netloc, parts.path, accept, permitted) == ("www.six-group.com", "/fqs/snap.json", "application/json", False)
    assert parse_qs(parts.query)["where"] == ["ValorId=CH0038863350CHF4|CH0009980894CHF9|CH0000000000CHF4"]
    nestle, smi = quotes
    # 17:36:44.267 in Zurich, in summer time until 25 October, winter time after: 16:36:44.267 UTC on 30 October.
    assert (nestle.ref, nestle.at, nestle.close, nestle.previous_close, nestle.volume) == (
        "CH0038863350CHF4", datetime(2026, 10, 30, 16, 36, 44, 267000, tzinfo=UTC), Decimal("79.5"), Decimal("79.1"), 3200000,
    )
    assert (smi.close, smi.open, smi.volume) == (Decimal("12001.5"), None, None)
    assert [each.ref for each in missing] == ["CH0000000000CHF4"] and missing[0].reason.startswith("Kein Kurs von SIX")


@pytest.mark.parametrize("body", [b"<html>", {"rows": []}])
def test_an_answer_that_is_no_table_of_prices_is_rejected(body):
    with pytest.raises(SourceRejected, match="Antwort von SIX"):
        six.latest(FakeHttp(body), ["CH0038863350CHF4"])
