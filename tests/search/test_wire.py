"""Tests for the wire-format parser shared by all FlightsFrontendService responses."""

import json

import pytest

from fli.search._wire import find_api_error, iter_wrb_chunks, parse_first_wrb_payload
from fli.search.exceptions import FlightsAPIError

# The exact body Google's GetShoppingResults now returns when it rejects a
# request (issue #200). Captured live 2026-06-14 for AMS->DXB. HTTP 200, ~325
# bytes, a ``travel.frontend.flights.ErrorResponse`` envelope rather than
# flight data. The wrb.fr row carries a null inner payload (row[2]) and the
# error marker + code at row[5].
ERROR_RESPONSE_BODY = (
    ")]}'\n\n"
    '[["wrb.fr",null,null,null,null,[13,null,'
    '[["type.googleapis.com/travel.frontend.flights.ErrorResponse",'
    "[[null,[[1781415240097441,139797767,1411962967],null,null,null,null,"
    '[[0]]],0,"SD0uaqH5BYfK1PIP17CjoQU",'
    '"H9YhYomwtAXsANeAwABG--------ejcxa15AAAAAGouPUgBlrsMA"],0]]]]],'
    '["di",43],["af.httprm",43,"8734836305667018155",6]]'
)


def _single_chunk(payload):
    """Build the legacy single-chunk response (no length headers)."""
    inner_json = json.dumps(payload, separators=(",", ":"))
    outer = [["wrb.fr", None, inner_json]]
    return ")]}'\n\n" + json.dumps(outer)


def _multi_chunk(*payloads):
    """Build a multi-chunk response with explicit length prefixes.

    Mirrors Google's actual format: each length header counts both the
    leading newline that follows the header AND the trailing newline that
    separates this chunk from the next (i.e. ``len(outer_json) + 1``).
    """
    parts = [")]}'\n\n"]
    for p in payloads:
        inner_json = json.dumps(p, separators=(",", ":"))
        outer_json = json.dumps([["wrb.fr", None, inner_json]], separators=(",", ":"))
        # The length header counts UTF-8 BYTES (not Python str chars) plus
        # the two surrounding newlines. Encoding the JSON before measuring
        # keeps the test correct when payloads contain non-ASCII characters
        # like accented airport names or Japanese carrier strings.
        byte_len = len(outer_json.encode("utf-8")) + 2
        parts.append(f"{byte_len}\n{outer_json}\n")
    return "".join(parts)


class TestIterWrbChunks:
    def test_single_chunk_legacy_format(self):
        body = _single_chunk([1, "hello", [2, 3]])
        chunks = list(iter_wrb_chunks(body))
        assert chunks == [[1, "hello", [2, 3]]]

    def test_multi_chunk_format_yields_both(self):
        body = _multi_chunk([1, "alpha"], [2, "beta"])
        chunks = list(iter_wrb_chunks(body))
        assert chunks == [[1, "alpha"], [2, "beta"]]

    def test_returns_nothing_for_empty_body(self):
        assert list(iter_wrb_chunks("")) == []

    def test_skips_non_wrb_rows(self):
        body = ")]}'\n\n" + json.dumps(
            [["di", 44], ["af.httprm", 43, "x", 32], ["wrb.fr", None, json.dumps([1])]]
        )
        assert list(iter_wrb_chunks(body)) == [[1]]

    def test_handles_malformed_inner_json_gracefully(self):
        body = ")]}'\n\n" + json.dumps([["wrb.fr", None, "{not valid"]])
        assert list(iter_wrb_chunks(body)) == []

    def test_non_ascii_chunk_payload(self):
        # The length header counts UTF-8 bytes, not characters — confirm a
        # payload with multi-byte chars round-trips correctly (regression
        # guard for the byte-vs-char-length bug in the test helper).
        body = _multi_chunk([1, "東京", "café", "résumé"])
        chunks = list(iter_wrb_chunks(body))
        assert chunks == [[1, "東京", "café", "résumé"]]


class TestParseFirstWrbPayload:
    def test_returns_first_chunk_only(self):
        body = _multi_chunk([1, "alpha"], [2, "beta"])
        assert parse_first_wrb_payload(body) == [1, "alpha"]

    def test_returns_none_when_empty(self):
        assert parse_first_wrb_payload("") is None


class TestIterWrbChunksEdgeCases:
    def test_bytes_input_works(self):
        body = _single_chunk([1, "hello"])
        chunks_str = list(iter_wrb_chunks(body))
        chunks_bytes = list(iter_wrb_chunks(body.encode("utf-8")))
        assert chunks_str == chunks_bytes

    def test_prefix_only_body_returns_nothing(self):
        # Body is only the JSONP prefix with no actual chunk data.
        assert list(iter_wrb_chunks(b")]}'\n\n")) == []

    def test_whitespace_only_body_returns_nothing(self):
        assert list(iter_wrb_chunks("   \n\n  ")) == []

    def test_malformed_length_header_truncates_stream(self):
        # A non-numeric length header causes the parser to stop cleanly.
        body = ")]}'\n\nabc\n[not parsed]"
        assert list(iter_wrb_chunks(body)) == []

    def test_outer_is_dict_not_list_is_skipped(self):
        body = ")]}'\n\n" + json.dumps({"key": "value"})
        assert list(iter_wrb_chunks(body)) == []

    def test_wrb_row_with_none_inner_skipped(self):
        body = ")]}'\n\n" + json.dumps([["wrb.fr", None, None]])
        assert list(iter_wrb_chunks(body)) == []

    def test_wrb_row_with_non_string_inner_skipped(self):
        body = ")]}'\n\n" + json.dumps([["wrb.fr", None, [1, 2, 3]]])
        assert list(iter_wrb_chunks(body)) == []

    def test_wrb_row_too_short_skipped(self):
        body = ")]}'\n\n" + json.dumps([["wrb.fr", None]])
        assert list(iter_wrb_chunks(body)) == []

    def test_non_wrb_rows_between_multi_chunks_ignored(self):
        parts = [")]}'\n\n"]
        for payload in [[1, "alpha"], [2, "beta"]]:
            inner_json = json.dumps(payload, separators=(",", ":"))
            # Mix wrb.fr row with a di row in each chunk's outer list.
            outer = [["di", 44], ["wrb.fr", None, inner_json]]
            outer_json = json.dumps(outer, separators=(",", ":"))
            byte_len = len(outer_json.encode("utf-8")) + 2
            parts.append(f"{byte_len}\n{outer_json}\n")
        body = "".join(parts)
        chunks = list(iter_wrb_chunks(body))
        assert chunks == [[1, "alpha"], [2, "beta"]]

    def test_multiple_wrb_chunks_all_yielded_with_mixed_rows(self):
        # Two separate multi-chunks, each with only wrb.fr rows.
        body = _multi_chunk([10], [20], [30])
        chunks = list(iter_wrb_chunks(body))
        assert chunks == [[10], [20], [30]]


class TestParseFirstWrbPayloadEdgeCases:
    def test_returns_none_for_only_non_wrb_rows(self):
        body = ")]}'\n\n" + json.dumps([["di", 44], ["af.httprm", 43, "x"]])
        assert parse_first_wrb_payload(body) is None

    def test_skips_invalid_inner_to_find_second_valid_chunk(self):
        # First wrb.fr row has an invalid inner JSON; second is valid.
        bad_inner = "{not valid"
        good_inner = json.dumps([42])
        outer = [["wrb.fr", None, bad_inner], ["wrb.fr", None, good_inner]]
        body = ")]}'\n\n" + json.dumps(outer)
        assert parse_first_wrb_payload(body) == [42]


class TestErrorResponseEnvelope:
    """Regression for issue #200 — a rejected GetShoppingResults request.

    Google replies HTTP 200 with a ``travel.frontend.flights.ErrorResponse``
    envelope instead of flight data. Previously this collapsed to ``None`` and
    the CLI/MCP reported ``success:true, count:0`` — a rejection masquerading
    as "no flights". The parser must now fail loud.
    """

    def test_find_api_error_detects_envelope(self):
        result = find_api_error(ERROR_RESPONSE_BODY)
        assert result is not None
        assert result.error_code == 13

    def test_find_api_error_none_for_normal_payload(self):
        body = _single_chunk([1, "real flight data"])
        assert find_api_error(body) is None

    def test_find_api_error_none_for_empty_body(self):
        assert find_api_error("") is None

    def test_parse_first_wrb_payload_raises_on_error_envelope(self):
        with pytest.raises(FlightsAPIError) as excinfo:
            parse_first_wrb_payload(ERROR_RESPONSE_BODY)
        assert excinfo.value.error_code == 13
        # The message must surface that this is a Google-side rejection so the
        # error never reads as an empty result.
        assert "reject" in str(excinfo.value).lower()

    def test_error_envelope_is_not_confused_with_valid_empty(self):
        # A valid-but-empty data chunk (real route, no flights) must still
        # return cleanly, never raise — only the ErrorResponse envelope raises.
        body = _single_chunk([None, [], []])
        assert parse_first_wrb_payload(body) == [None, [], []]

    def test_data_chunk_takes_precedence_over_trailing_error_marker(self):
        # If a decodable data chunk is present it wins; we never raise when
        # real flight data came back.
        good = json.dumps([1, "data"])
        outer = [["wrb.fr", None, good]]
        body = ")]}'\n\n" + json.dumps(outer)
        assert parse_first_wrb_payload(body) == [1, "data"]
