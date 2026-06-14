"""Live regression for issue #200 — shopping search must return real flights.

Before the fix, ``GetShoppingResults`` rejections (transient ``ErrorResponse``
envelopes) collapsed into an empty ``success:true, count:0`` result. The fix
retries the transient rejection and otherwise fails loud, so a trunk route
should reliably yield flights.

Gated behind ``FLI_LIVE_TESTS=1`` — it hits Google's live endpoint, so the
default ``pytest`` run (and CI without network) skips it.
"""

import os
from datetime import datetime, timedelta

import pytest

from fli.models import (
    Airport,
    FlightSearchFilters,
    FlightSegment,
    MaxStops,
    PassengerInfo,
    SeatType,
    SortBy,
    TripType,
)
from fli.search import SearchFlights

pytestmark = pytest.mark.skipif(
    os.environ.get("FLI_LIVE_TESTS") != "1",
    reason="live network test; set FLI_LIVE_TESTS=1 to run",
)


def _date(days_ahead: int) -> str:
    return (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


def test_trunk_route_returns_flights():
    filters = FlightSearchFilters(
        trip_type=TripType.ONE_WAY,
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport.AMS, 0]],
                arrival_airport=[[Airport.DXB, 0]],
                travel_date=_date(40),
            )
        ],
        seat_type=SeatType.ECONOMY,
        stops=MaxStops.ANY,
        sort_by=SortBy.CHEAPEST,
    )

    results = SearchFlights().search(filters)

    assert results is not None, "search returned None — regression of issue #200"
    assert len(results) > 0
    first = results[0]
    assert first.legs, "result has no legs"
    assert first.price is not None
