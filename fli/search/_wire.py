r"""Parsing helpers for Google Flights' FlightsFrontendService wire format.

The Service returns JSONP-flavoured responses of the form::

    )]}'\n\n
    <chunk1_byte_len>\n
    [["wrb.fr", null, "<inner JSON string>"]]
    <chunk2_byte_len>\n
    [["wrb.fr", null, "<inner JSON string>"]]
    ...

`GetShoppingResults` and `GetCalendarGraph` happen to emit a single chunk so
the legacy parsers in this package could get away with `lstrip(")]}'")`.
`GetBookingResults` emits two chunks, so we need a proper multi-chunk reader.

Important quirk: the length headers count UTF-8 **bytes**, not Python string
characters. When the response contains any non-ASCII characters (which it
sometimes does — airport names, airline names) the offsets diverge, so the
reader must operate over the byte representation of the body.

This module centralises that reader and exposes :func:`iter_wrb_chunks` which
yields the decoded inner JSON of each ``wrb.fr`` chunk.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from fli.search.exceptions import FlightsAPIError

logger = logging.getLogger(__name__)

_PREFIX = b")]}'"

# Type URL Google stamps on a rejected FlightsFrontendService RPC. When this
# appears in a ``wrb.fr`` row's error slot (row[5]) the request was rejected
# server-side rather than answered with flight data — see issue #200.
_ERROR_TYPE_MARKER = "type.googleapis.com/travel.frontend.flights.ErrorResponse"


def _iter_outer_payloads(body: str | bytes) -> Iterator[Any]:
    """Yield each decoded top-level chunk list from a JSONP-flavoured body.

    Handles both the legacy single-chunk shape (no length headers, used by
    ``GetShoppingResults`` / ``GetCalendarGraph``) and the length-prefixed
    multi-chunk shape (``GetBookingResults``). Unlike :func:`iter_wrb_chunks`
    this yields the *raw* outer lists (including rows whose inner payload is
    null), so callers can inspect error envelopes as well as data.
    """
    if isinstance(body, str):
        raw = body.encode("utf-8")
    else:
        raw = body

    raw = raw.lstrip()
    if raw.startswith(_PREFIX):
        raw = raw[len(_PREFIX) :]
    raw = raw.lstrip()

    if not raw:
        return

    # Fast path: no length headers (legacy single-chunk responses).
    if not (b"0" <= raw[:1] <= b"9"):
        try:
            yield json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Failed to decode single-chunk wrb.fr body as JSON", exc_info=True)
        return

    cursor = 0
    while cursor < len(raw):
        # Read the decimal length prefix terminated by \n.
        end = raw.find(b"\n", cursor)
        if end == -1:
            break
        try:
            length = int(raw[cursor:end])
        except ValueError:
            logger.warning(
                "Malformed length header at offset %d; truncating chunk stream",
                cursor,
            )
            break
        # Google's length header counts the leading newline after the header
        # AND the trailing newline that separates this chunk from the next.
        # We've already consumed the leading newline (it terminated the header),
        # so we read `length - 1` bytes which gives JSON + trailing \n.
        cursor = end + 1
        chunk_bytes = max(length - 1, 0)
        payload = raw[cursor : cursor + chunk_bytes]
        cursor += chunk_bytes
        try:
            yield json.loads(payload.strip().decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Discarding malformed wrb.fr chunk", exc_info=True)
            continue


def iter_wrb_chunks(body: str | bytes) -> Iterator[Any]:
    """Yield the inner JSON object of every ``wrb.fr`` chunk in ``body``.

    Robust to single-chunk responses with no length headers (the older
    ``GetShoppingResults`` / ``GetCalendarGraph`` shape) — those are parsed
    by falling back to a single JSON load over the trimmed body.
    """
    for outer in _iter_outer_payloads(body):
        yield from _chunks_from_outer(outer)


def find_api_error(body: str | bytes) -> FlightsAPIError | None:
    """Return a :class:`FlightsAPIError` if ``body`` is a rejection envelope.

    Google replies HTTP 200 with a ``travel.frontend.flights.ErrorResponse``
    envelope when it rejects a request (issue #200). The ``wrb.fr`` row then
    carries a *null* inner payload (so :func:`iter_wrb_chunks` yields nothing)
    and the error marker plus a status code at ``row[5]``. This walks the raw
    rows to surface that as a typed error instead of a silent empty result.

    Returns the built (but un-raised) error so callers decide when to raise;
    returns ``None`` for any normal payload (including a valid-but-empty one).
    """
    for outer in _iter_outer_payloads(body):
        if not isinstance(outer, list):
            continue
        for row in outer:
            if not (isinstance(row, list) and len(row) >= 6 and row[0] == "wrb.fr"):
                continue
            error_slot = row[5]
            if not _has_error_marker(error_slot):
                continue
            # row[5][0] is Google's status code (observed 13 == INTERNAL).
            code = error_slot[0] if isinstance(error_slot[0], int) else None
            request_id = _extract_request_id(outer)
            return FlightsAPIError(
                "Google Flights rejected the request "
                f"(ErrorResponse, code={code}). This is typically transient "
                "rate-limiting / anti-abuse throttling of automated traffic "
                "(issue #200) — the request payload is valid and the same "
                "search usually succeeds on retry.",
                error_code=code,
                request_id=request_id,
            )
    return None


def _has_error_marker(error_slot: Any) -> bool:
    """Return True if ``error_slot`` contains the ErrorResponse type-URL marker."""
    if not isinstance(error_slot, list):
        return False
    # The marker is nested a few levels deep and the exact shape may drift;
    # a substring check over the serialised slot is robust to reordering.
    try:
        return _ERROR_TYPE_MARKER in json.dumps(error_slot)
    except (TypeError, ValueError):
        return False


def _extract_request_id(outer: list) -> str | None:
    """Pull the request id from the trailing ``af.httprm`` row, if present."""
    for row in outer:
        if isinstance(row, list) and len(row) >= 3 and row[0] == "af.httprm":
            candidate = row[2]
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


def _chunks_from_outer(outer: Any) -> Iterator[Any]:
    """Walk a top-level chunk list and yield decoded inner-JSON payloads."""
    if not isinstance(outer, list):
        return
    for row in outer:
        if not isinstance(row, list) or len(row) < 3:
            continue
        if row[0] != "wrb.fr":
            continue
        inner = row[2]
        if not isinstance(inner, str) or not inner:
            continue
        try:
            yield json.loads(inner)
        except (ValueError, json.JSONDecodeError):
            logger.warning("Failed to decode wrb.fr inner JSON payload", exc_info=True)
            continue


def parse_first_wrb_payload(body: str | bytes) -> Any:
    """Return the inner JSON of the first ``wrb.fr`` chunk, or None.

    Raises:
        FlightsAPIError: ``body`` is a ``travel.frontend.flights.ErrorResponse``
            rejection envelope (issue #200) rather than flight data. This keeps
            a server-side rejection from collapsing into a silent empty result.

    """
    for chunk in iter_wrb_chunks(body):
        return chunk
    # No decodable data chunk. Distinguish a server-side rejection (raise) from
    # a genuinely empty/absent body (return None) so callers fail loud only on
    # an actual error envelope.
    error = find_api_error(body)
    if error is not None:
        raise error
    return None
