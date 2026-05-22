from allstak.propagation import merge_baggage, set_mapping_headers


def test_merge_baggage_preserves_non_allstak_values_and_replaces_allstak_values():
    merged = merge_baggage(
        "vendor=value, allstak-trace_id=old, allstak-span_id=old",
        "t" * 32,
        "r" * 32,
        "s" * 16,
    )

    assert merged == (
        "vendor=value,"
        f"allstak-trace_id={'t' * 32},"
        f"allstak-request_id={'r' * 32},"
        f"allstak-span_id={'s' * 16}"
    )


def test_set_mapping_headers_sets_traceparent_and_baggage_without_overwriting_existing_trace_header():
    headers = {"x-allstak-trace-id": "existing", "baggage": "vendor=value"}

    set_mapping_headers(
        headers,
        trace_id="t" * 32,
        request_id="r" * 32,
        span_id="s" * 16,
        overwrite=False,
    )

    assert headers["x-allstak-trace-id"] == "existing"
    assert headers["traceparent"] == f"00-{'t' * 32}-{'s' * 16}-01"
    assert headers["baggage"] == (
        "vendor=value,"
        f"allstak-trace_id={'t' * 32},"
        f"allstak-request_id={'r' * 32},"
        f"allstak-span_id={'s' * 16}"
    )
    assert headers["allstak-baggage"] == (
        f"allstak-trace_id={'t' * 32},"
        f"allstak-request_id={'r' * 32},"
        f"allstak-span_id={'s' * 16}"
    )
