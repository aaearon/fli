# Search API Reference

## Flight Search

The main search functionality for finding specific flights.

### SearchFlights

::: fli.search.flights.SearchFlights

### FlightSearchFilters

A simplified interface for flight search parameters.

::: fli.models.google_flights.FlightSearchFilters

## Date Search

Search functionality for finding the cheapest dates to fly.

### SearchDates

::: fli.search.dates.SearchDates

### DatePrice

::: fli.search.dates.DatePrice

## Examples

### Basic Flight Search

```python
from fli.search import SearchFlights
from fli.models import Airport, SeatType, FlightSearchFilters, FlightSegment, PassengerInfo

# Create filters
filters = FlightSearchFilters(
    passenger_info=PassengerInfo(adults=1),
    flight_segments=[
        FlightSegment(
            departure_airport=[[Airport.JFK, 0]],
            arrival_airport=[[Airport.LAX, 0]],
            travel_date="2026-06-01",
        )
    ],
    seat_type=SeatType.ECONOMY
)

# Search flights
search = SearchFlights()
results = search.search(filters)
```

### Date Range Search

```python
from fli.search import SearchDates
from fli.models import DateSearchFilters, Airport, FlightSegment, PassengerInfo

# Create filters
filters = DateSearchFilters(
    passenger_info=PassengerInfo(adults=1),
    flight_segments=[
        FlightSegment(
            departure_airport=[[Airport.JFK, 0]],
            arrival_airport=[[Airport.LAX, 0]],
            travel_date="2026-06-01",
        )
    ],
    from_date="2026-06-01",
    to_date="2026-06-30"
)

# Search dates
search = SearchDates()
results = search.search(filters)
```

### Running These Examples

You can find complete, runnable versions of these examples in the [`examples/python/`](https://github.com/punitarani/fli/tree/main/examples/python) directory:

```bash
# Run with uv (recommended)
uv run python examples/python/basic_one_way_search.py
uv run python examples/python/date_range_search.py

# Or install dependencies and run directly
pip install pydantic curl_cffi httpx
python examples/python/basic_one_way_search.py
```

For more advanced examples, see:

* `examples/python/complex_flight_search.py` - Advanced filtering
* `examples/python/result_processing.py` - Data analysis
* `examples/python/error_handling_with_retries.py` - Robust error handling

## Error Handling

### Rejected shopping searches (issue #200)

Google's `GetShoppingResults` endpoint intermittently rejects a request with
an HTTP 200 `travel.frontend.flights.ErrorResponse` envelope instead of flight
data. This is transient anti-abuse / rate-limiting of automated traffic from a
single egress IP — the request payload itself is valid, and the same search
typically succeeds on retry.

`SearchFlights.search()` handles this in two ways:

* **Bounded retry** — the primary call is retried a few times on a rejection
  (a cold process's first request fails disproportionately often, and the
  retry carries the cookie the first response set). Parallel round-trip
  expansion calls are *not* retried, to avoid compounding the throttling.
* **Fail loud** — if every attempt is rejected, a
  [`FlightsAPIError`][fli.search.exceptions.FlightsAPIError] is raised rather
  than returning an empty result. A rejection never masquerades as
  `success:true, count:0`.

```python
from fli.search import SearchFlights
from fli.search.exceptions import FlightsAPIError

try:
    results = SearchFlights().search(filters)
except FlightsAPIError as e:
    # Google rejected the request (code in e.error_code). Usually transient —
    # back off and retry, or run through a proxy / different egress IP.
    print(f"Rejected by Google Flights (code={e.error_code}); try again shortly")
```

The retry budget is tunable via environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `FLI_SHOPPING_MAX_ATTEMPTS` | Total attempts for the primary shopping call (`1` = fail loud on first rejection) | `3` |
| `FLI_SHOPPING_RETRY_DELAY` | Base backoff in seconds (multiplied by attempt number) | `1.0` |
| `FLI_SHOPPING_RETRY_JITTER` | Extra random backoff (0–N seconds) added per retry | `1.0` |

For high-volume use, route requests through rotating proxies and pace each
egress IP slowly rather than raising the per-call retry count.

## HTTP Client

The underlying HTTP client used for API requests.

### Client

::: fli.search.client.Client
