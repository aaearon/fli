"""Typed errors raised by the search client.

These exist so the CLI (and library consumers) can react to network
failures with a clear, user-facing message instead of a raw curl-cffi
traceback. They are intentionally light wrappers — the original
exception is kept as ``__cause__`` for logging.
"""

from __future__ import annotations


class SearchClientError(Exception):
    """Base class for errors talking to the Google Flights backend."""


class SearchTimeoutError(SearchClientError):
    """The request to Google Flights timed out before any data arrived."""


class SearchConnectionError(SearchClientError):
    """A network/DNS issue prevented us from reaching Google Flights."""


class SearchHTTPError(SearchClientError):
    """Google Flights returned a non-2xx HTTP response."""

    def __init__(self, message: str, *, status_code: int | None = None):
        """Store the HTTP status alongside the message for richer logging."""
        super().__init__(message)
        self.status_code = status_code


class SearchParseError(SearchClientError):
    """A successful (2xx) HTTP response could not be parsed into flights.

    Distinct from the network / HTTP errors above — Google *did* respond,
    but the body wasn't the shape we expected. Use this to tell "Google
    responded but the shape changed" apart from "Google didn't respond at
    all".

    Defined here (rather than in :mod:`fli.search.flights`) so the
    low-level wire reader in :mod:`fli.search._wire` can raise it without a
    circular import. :mod:`fli.search.flights` re-exports it for
    backwards compatibility.
    """


class FlightsAPIError(SearchParseError):
    """Google rejected the RPC with a ``ErrorResponse`` envelope.

    The FlightsFrontendService accepted the HTTP request (200 OK) but the
    RPC itself was rejected server-side: the body is a
    ``travel.frontend.flights.ErrorResponse`` instead of flight data
    (issue #200). In practice this is transient anti-abuse / rate-limiting
    of automated ``GetShoppingResults`` traffic from a single egress IP —
    the request *payload* is valid (the identical request, and the
    browser, succeed), and a retry typically clears it.

    Surfacing this as a typed error is the fix's second half: a rejection
    must never collapse into an empty ``success:true, count:0`` result.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: int | None = None,
        request_id: str | None = None,
    ):
        """Store Google's error code and request id for logging / display."""
        super().__init__(message)
        self.error_code = error_code
        self.request_id = request_id
