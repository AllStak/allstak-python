from allstak.propagation import merge_baggage, parse_traceparent, set_mapping_headers


TRACE_ID = "a" * 32
SPAN_ID = "b" * 16
REQUEST_ID = "c" * 32


def test_merge_baggage_preserves_non_allstak_values_and_replaces_allstak_values():
    merged = merge_baggage(
        "vendor=value, allstak-trace_id=old, allstak-span_id=old",
        TRACE_ID,
        REQUEST_ID,
        SPAN_ID,
    )

    assert merged == (
        "vendor=value,"
        f"allstak-trace_id={TRACE_ID},"
        f"allstak-request_id={REQUEST_ID},"
        f"allstak-span_id={SPAN_ID}"
    )


def test_set_mapping_headers_sets_traceparent_and_baggage_without_overwriting_existing_trace_header():
    headers = {"x-allstak-trace-id": "existing", "baggage": "vendor=value"}

    set_mapping_headers(
        headers,
        trace_id=TRACE_ID,
        request_id=REQUEST_ID,
        span_id=SPAN_ID,
        overwrite=False,
    )

    assert headers["x-allstak-trace-id"] == "existing"
    assert headers["traceparent"] == f"00-{TRACE_ID}-{SPAN_ID}-01"
    assert headers["baggage"] == (
        "vendor=value,"
        f"allstak-trace_id={TRACE_ID},"
        f"allstak-request_id={REQUEST_ID},"
        f"allstak-span_id={SPAN_ID}"
    )
    assert headers["allstak-baggage"] == (
        f"allstak-trace_id={TRACE_ID},"
        f"allstak-request_id={REQUEST_ID},"
        f"allstak-span_id={SPAN_ID}"
    )


def test_parse_traceparent_accepts_valid_w3c_header():
    assert parse_traceparent(f"00-{TRACE_ID}-{SPAN_ID}-01") == (TRACE_ID, SPAN_ID, True)


def test_parse_traceparent_rejects_invalid_headers():
    assert parse_traceparent(f"00-{TRACE_ID}-{'0' * 16}-01") is None
    assert parse_traceparent(f"00-{'g' * 32}-{SPAN_ID}-01") is None
    assert parse_traceparent(f"00-{TRACE_ID}-{'d' * 32}-01") is None
