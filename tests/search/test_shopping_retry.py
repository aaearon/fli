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

    result = search._post_and_parse("https://x", "f.req=...", max_attempts=3)

    assert result == [1, "data"]
    assert search.client.calls == 3


def test_fails_loud_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(flights_module, "SHOPPING_MAX_ATTEMPTS", 3)
    search = SearchFlights()
    search.client = _ScriptedClient([ERROR_BODY, ERROR_BODY, ERROR_BODY])

    with pytest.raises(FlightsAPIError) as excinfo:
        search._post_and_parse("https://x", "f.req=...", max_attempts=3)

    assert excinfo.value.error_code == 13
    assert excinfo.value.request_id == "req-12345"
    assert search.client.calls == 3


def test_single_attempt_does_not_retry():
    # Expansion workers pass max_attempts=1 — one rejection raises immediately.
    search = SearchFlights()
    search.client = _ScriptedClient([ERROR_BODY])

    with pytest.raises(FlightsAPIError):
        search._post_and_parse("https://x", "f.req=...", max_attempts=1)

    assert search.client.calls == 1


def test_valid_empty_body_returns_none_without_retry():
    # A genuinely empty (but well-formed) body is not an error — return None
    # immediately, never burning retries on it.
    search = SearchFlights()
    search.client = _ScriptedClient([")]}'\n\n" + json.dumps([["di", 44]])])

    assert search._post_and_parse("https://x", "f.req=...", max_attempts=3) is None
    assert search.client.calls == 1
