"""Retry / fail-loud behaviour for the GetShoppingResults call (issue #200).

Google now intermittently rejects the shopping RPC with a transient
``travel.frontend.flights.ErrorResponse`` envelope. ``SearchFlights`` retries
the primary call a bounded number of times and, if every attempt is rejected,
fails loud with a :class:`FlightsAPIError` instead of returning an empty
``success:true, count:0`` result.
"""

import json

import pytest

from fli.search import flights as flights_module
from fli.search.exceptions import FlightsAPIError
from fli.search.flights import SearchFlights

# A rejection envelope (issue #200) — null inner payload + ErrorResponse marker.
ERROR_BODY = (
    ")]}'\n\n"
    '[["wrb.fr",null,null,null,null,[13,null,'
    '[["type.googleapis.com/travel.frontend.flights.ErrorResponse",'
    '[[null,[[1,2,3],null,null,null,null,[[0]]],0,"sid","rid"],0]]]]],'
    '["af.httprm",43,"req-12345",6]]'
)


def _ok_body(inner):
    """Build a legacy single-chunk success body wrapping ``inner``."""
    outer = [["wrb.fr", None, json.dumps(inner, separators=(",", ":"))]]
    return ")]}'\n\n" + json.dumps(outer)


class _FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class _ScriptedClient:
    """Returns a queued sequence of response bodies, one per ``post`` call."""

    def __init__(self, bodies):
        self._bodies = list(bodies)
        self.calls = 0

    def post(self, **kwargs):
        self.calls += 1
        body = self._bodies.pop(0)
        return _FakeResponse(body)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Keep retries instant in tests.
    monkeypatch.setattr(flights_module.time, "sleep", lambda _s: None)


def test_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(flights_module, "SHOPPING_MAX_ATTEMPTS", 3)
    search = SearchFlights()
    search.client = _ScriptedClient([ERROR_BODY, ERROR_BODY, _ok_body([1, "data"])])

    result = search._post_and_parse("https://x", "ENCODED", max_attempts=3)

    assert result == [1, "data"]
    assert search.client.calls == 3


def test_fails_loud_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(flights_module, "SHOPPING_MAX_ATTEMPTS", 3)
    search = SearchFlights()
    search.client = _ScriptedClient([ERROR_BODY, ERROR_BODY, ERROR_BODY])

    with pytest.raises(FlightsAPIError) as excinfo:
        search._post_and_parse("https://x", "ENCODED", max_attempts=3)

    assert excinfo.value.error_code == 13
    assert excinfo.value.request_id == "req-12345"
    assert search.client.calls == 3


def test_single_attempt_does_not_retry():
    # Expansion workers pass max_attempts=1 — one rejection raises immediately.
    search = SearchFlights()
    search.client = _ScriptedClient([ERROR_BODY])

    with pytest.raises(FlightsAPIError):
        search._post_and_parse("https://x", "ENCODED", max_attempts=1)

    assert search.client.calls == 1


def test_valid_empty_body_returns_none_without_retry():
    # A genuinely empty (but well-formed) body is not an error — return None
    # immediately, never burning retries on it.
    search = SearchFlights()
    search.client = _ScriptedClient([")]}'\n\n" + json.dumps([["di", 44]])])

    assert search._post_and_parse("https://x", "ENCODED", max_attempts=3) is None
    assert search.client.calls == 1


def test_env_float_falls_back_on_garbage(monkeypatch):
    # Malformed tuning knobs must not crash import — fall back to the default.
    monkeypatch.setenv("FLI_SHOPPING_RETRY_DELAY", "not-a-number")
    assert flights_module._env_float("FLI_SHOPPING_RETRY_DELAY", 2.5) == 2.5
    monkeypatch.setenv("FLI_SHOPPING_RETRY_DELAY", "-1")
    assert flights_module._env_float("FLI_SHOPPING_RETRY_DELAY", 2.5) == 2.5
    monkeypatch.delenv("FLI_SHOPPING_RETRY_DELAY", raising=False)
    assert flights_module._env_float("FLI_SHOPPING_RETRY_DELAY", 2.5) == 2.5
    monkeypatch.setenv("FLI_SHOPPING_RETRY_DELAY", "3.5")
    assert flights_module._env_float("FLI_SHOPPING_RETRY_DELAY", 2.5) == 3.5


def test_dates_multichunk_tolerates_one_rejected_chunk(monkeypatch):
    """A rejected chunk in a multi-chunk date search is skipped, not fatal."""
    from datetime import datetime, timedelta

    from fli.models import (
        Airport,
        DateSearchFilters,
        FlightSegment,
        PassengerInfo,
    )
    from fli.search.dates import DatePrice, SearchDates

    def _future(days):
        return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

    # > MAX_DAYS_PER_SEARCH (61) so search() chunks and uses parallel_map.
    filters = DateSearchFilters(
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport.JFK, 0]],
                arrival_airport=[[Airport.LAX, 0]],
                travel_date=_future(10),
            )
        ],
        from_date=_future(10),
        to_date=_future(180),
    )

    good = [DatePrice(date=(datetime(2026, 7, 15),), price=199.0)]

    def _fake_chunk(self, cf, **kwargs):
        # First chunk is rejected; later chunks return data.
        if _fake_chunk.calls == 0:
            _fake_chunk.calls += 1
            raise FlightsAPIError("rejected", error_code=13)
        _fake_chunk.calls += 1
        return good

    _fake_chunk.calls = 0

    monkeypatch.setattr(SearchDates, "_search_chunk", _fake_chunk)
    results = SearchDates().search(filters)

    # The rejected chunk is skipped; the surviving chunks' prices come back.
    assert _fake_chunk.calls >= 2, "range should have split into multiple chunks"
    assert results is not None and len(results) >= 1
    assert all(r == good[0] for r in results)
