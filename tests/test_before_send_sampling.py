"""Tests for before_send callback and error/trace sampling."""

from typing import Any, Dict, Optional
from unittest import mock

import pytest

from allstak.config import AllStakConfig
from allstak.modules.errors import ErrorModule
from allstak.modules.tracing import TracingModule
from allstak.propagation import set_mapping_headers


class FakeTransport:
    """Records every payload handed to post()."""

    def __init__(self):
        self.posts = []

    def post(self, path: str, payload: Dict[str, Any]):
        self.posts.append((path, payload))
        return 202, {"data": {"id": "evt_123"}}


def _module(**config_kwargs) -> tuple[ErrorModule, FakeTransport]:
    transport = FakeTransport()
    config = AllStakConfig(api_key="ask_test", **config_kwargs)
    return ErrorModule(transport, config), transport  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# before_send
# --------------------------------------------------------------------------

def test_before_send_can_modify_event():
    def before_send(event: Dict[str, Any]) -> Dict[str, Any]:
        event["message"] = "redacted"
        return event

    mod, transport = _module(before_send=before_send)
    mod.capture_error("ValueError", "secret message")

    assert len(transport.posts) == 1
    _, payload = transport.posts[0]
    assert payload["message"] == "redacted"


def test_before_send_can_drop_event():
    def before_send(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return None  # drop

    mod, transport = _module(before_send=before_send)
    result = mod.capture_error("ValueError", "drop me")

    assert result is None
    assert transport.posts == []


def test_before_send_exception_is_fail_open():
    def before_send(event: Dict[str, Any]) -> Dict[str, Any]:
        raise RuntimeError("user callback crashed")

    mod, transport = _module(before_send=before_send)
    mod.capture_error("ValueError", "still sent")

    # Fail-open: the original event is still sent despite the callback raising.
    assert len(transport.posts) == 1
    _, payload = transport.posts[0]
    assert payload["message"] == "still sent"


def test_before_send_receives_message_and_exception_events():
    seen = []

    def before_send(event: Dict[str, Any]) -> Dict[str, Any]:
        seen.append(event["exceptionClass"])
        return event

    mod, _ = _module(before_send=before_send)
    mod.capture_error("CustomError", "msg")
    try:
        raise ValueError("exc path")
    except ValueError as e:
        mod.capture_exception(e)

    assert "CustomError" in seen
    assert "ValueError" in seen


def test_final_sanitization_after_before_send_blocks_reintroduced_secrets():
    canary = "should_not_leak"
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature"

    def before_send(event: Dict[str, Any]) -> Dict[str, Any]:
        event["metadata"] = {
            "Authorization": f"Bearer {canary}",
            "Cookie": f"sid={canary}",
            "nested": {
                "password": canary,
                "apiKey": canary,
                "jwt": jwt,
                "values": [f"Bearer {canary}", {"secret": canary}],
            },
            "card": "4111111111111111",
        }
        event["breadcrumbs"] = [
            {"type": "default", "message": f"Bearer {canary}", "data": {"token": canary}}
        ]
        event["fingerprint"] = [f"Bearer {canary}"]
        return event

    mod, transport = _module(before_send=before_send)
    mod.capture_error("ValueError", "hook secret")

    _, payload = transport.posts[0]
    raw = str(payload)
    assert canary not in raw
    assert jwt not in raw
    assert "4111111111111111" not in raw
    assert payload["metadata"]["Authorization"] == "[REDACTED]"
    assert payload["metadata"]["Cookie"] == "[REDACTED]"
    assert payload["metadata"]["nested"]["password"] == "[REDACTED]"
    assert payload["metadata"]["nested"]["apiKey"] == "[REDACTED]"
    assert payload["metadata"]["nested"]["jwt"] == "[REDACTED]"
    assert payload["metadata"]["nested"]["values"][0] == "[REDACTED]"
    assert payload["metadata"]["nested"]["values"][1]["secret"] == "[REDACTED]"
    assert payload["metadata"]["card"] == "[REDACTED]"
    assert payload["breadcrumbs"][0]["message"] == "[REDACTED]"
    assert payload["breadcrumbs"][0]["data"]["token"] == "[REDACTED]"
    assert payload["fingerprint"][0] == "[REDACTED]"


# --------------------------------------------------------------------------
# sample_rate
# --------------------------------------------------------------------------

def test_sample_rate_zero_drops_all():
    mod, transport = _module(sample_rate=0.0)
    for _ in range(20):
        mod.capture_error("ValueError", "x")
    assert transport.posts == []


def test_sample_rate_one_keeps_all():
    mod, transport = _module(sample_rate=1.0)
    for _ in range(20):
        mod.capture_error("ValueError", "x")
    assert len(transport.posts) == 20


def test_sample_rate_drop_happens_before_before_send():
    """A dropped (sampled-out) event must never reach before_send."""
    calls = []

    def before_send(event):
        calls.append(event)
        return event

    mod, transport = _module(sample_rate=0.0, before_send=before_send)
    mod.capture_error("ValueError", "x")

    assert calls == []  # before_send never invoked for dropped event
    assert transport.posts == []


def test_sample_rate_uses_random(monkeypatch):
    # random.random() returns 0.9; with sample_rate 0.5, 0.9 >= 0.5 -> drop.
    import allstak.modules.errors as errmod

    monkeypatch.setattr(errmod.random, "random", lambda: 0.9)
    mod, transport = _module(sample_rate=0.5)
    mod.capture_error("ValueError", "x")
    assert transport.posts == []

    # random.random() returns 0.1; 0.1 >= 0.5 is False -> keep.
    monkeypatch.setattr(errmod.random, "random", lambda: 0.1)
    mod.capture_error("ValueError", "x")
    assert len(transport.posts) == 1


# --------------------------------------------------------------------------
# traces_sample_rate drives the traceparent sampled flag
# --------------------------------------------------------------------------

def _tracing(**config_kwargs) -> TracingModule:
    transport = FakeTransport()
    config = AllStakConfig(api_key="ask_test", **config_kwargs)
    return TracingModule(transport, config)  # type: ignore[arg-type]


def test_traces_sample_rate_none_is_always_sampled():
    tracing = _tracing()  # traces_sample_rate defaults to None
    assert tracing.is_sampled() is True
    span = tracing.start_span("op")
    assert span.sampled is True


def test_traces_sample_rate_zero_is_not_sampled():
    tracing = _tracing(traces_sample_rate=0.0)
    assert tracing.is_sampled() is False
    span = tracing.start_span("op")
    assert span.sampled is False


def test_traces_sample_rate_one_is_sampled():
    tracing = _tracing(traces_sample_rate=1.0)
    assert tracing.is_sampled() is True


def test_traceparent_flag_reflects_sampled_decision():
    sampled_headers: Dict[str, str] = {}
    set_mapping_headers(
        sampled_headers,
        trace_id="a" * 32,
        span_id="b" * 16,
        sampled=True,
    )
    assert sampled_headers["traceparent"].endswith("-01")

    unsampled_headers: Dict[str, str] = {}
    set_mapping_headers(
        unsampled_headers,
        trace_id="a" * 32,
        span_id="b" * 16,
        sampled=False,
    )
    assert unsampled_headers["traceparent"].endswith("-00")


def test_traceparent_default_is_sampled_for_backward_compat():
    headers: Dict[str, str] = {}
    set_mapping_headers(headers, trace_id="a" * 32, span_id="b" * 16)
    assert headers["traceparent"].endswith("-01")


def test_unsampled_spans_are_not_buffered():
    transport = FakeTransport()
    config = AllStakConfig(api_key="ask_test", traces_sample_rate=0.0)
    tracing = TracingModule(transport, config)  # type: ignore[arg-type]

    span = tracing.start_span("op")
    span.finish()
    tracing.flush()
    # Nothing should have been sent for an unsampled trace.
    assert transport.posts == []
    tracing.shutdown()


def test_mechanism_reaches_wire_payload():
    mod, transport = _module()
    try:
        raise ValueError("unhandled")
    except ValueError as e:
        mod.capture_exception(e, mechanism={"type": "excepthook", "handled": False})

    _, payload = transport.posts[0]
    assert payload["mechanism"] == "excepthook"
    assert payload["handled"] is False
