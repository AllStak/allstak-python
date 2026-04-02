"""Unit tests for DTO models — no network calls."""

import json
import pytest

from allstak.models.errors import ErrorPayload, UserContext, ERROR_LEVELS
from allstak.models.logs import LogPayload, LOG_LEVELS
from allstak.models.http_requests import HttpRequestItem, HttpRequestBatch
from allstak.models.replay import ReplayEvent, ReplayPayload
from allstak.models.heartbeat import HeartbeatPayload


# ---------------------------------------------------------------------------
# ErrorPayload
# ---------------------------------------------------------------------------

class TestErrorPayload:
    def test_minimal_required_fields(self):
        p = ErrorPayload(exception_class="ValueError", message="bad value")
        d = p.to_dict()
        assert d["exceptionClass"] == "ValueError"
        assert d["message"] == "bad value"
        assert d["level"] == "error"  # default

    def test_all_fields(self):
        user = UserContext(id="u1", email="a@b.com", ip="1.2.3.4")
        p = ErrorPayload(
            exception_class="TypeError",
            message="type err",
            stack_trace=["frame1", "frame2"],
            level="fatal",
            environment="staging",
            release="v1.0",
            session_id="sess-123",
            user=user,
            metadata={"key": "val"},
        )
        d = p.to_dict()
        assert d["exceptionClass"] == "TypeError"
        assert d["stackTrace"] == ["frame1", "frame2"]
        assert d["level"] == "fatal"
        assert d["environment"] == "staging"
        assert d["release"] == "v1.0"
        assert d["sessionId"] == "sess-123"
        assert d["user"]["id"] == "u1"
        assert d["user"]["email"] == "a@b.com"
        assert d["metadata"] == {"key": "val"}

    def test_invalid_level_raises(self):
        p = ErrorPayload(exception_class="X", message="y", level="trace")
        with pytest.raises(ValueError, match="Invalid error level"):
            p.to_dict()

    def test_all_valid_levels(self):
        for level in ERROR_LEVELS:
            p = ErrorPayload(exception_class="E", message="m", level=level)
            d = p.to_dict()
            assert d["level"] == level

    def test_empty_stack_trace_omitted(self):
        p = ErrorPayload(exception_class="E", message="m", stack_trace=[])
        d = p.to_dict()
        assert "stackTrace" not in d

    def test_user_context_partial(self):
        user = UserContext(email="x@y.com")
        p = ErrorPayload(exception_class="E", message="m", user=user)
        d = p.to_dict()
        assert "id" not in d["user"]
        assert d["user"]["email"] == "x@y.com"


# ---------------------------------------------------------------------------
# LogPayload
# ---------------------------------------------------------------------------

class TestLogPayload:
    def test_minimal(self):
        p = LogPayload(level="info", message="hello")
        d = p.to_dict()
        assert d == {"level": "info", "message": "hello"}

    def test_all_valid_levels(self):
        for level in LOG_LEVELS:
            p = LogPayload(level=level, message="test")
            assert p.to_dict()["level"] == level

    def test_warning_is_invalid_for_logs(self):
        p = LogPayload(level="warning", message="test")
        with pytest.raises(ValueError, match="warn"):
            p.to_dict()

    def test_with_metadata(self):
        p = LogPayload(
            level="warn",
            message="slow query",
            service="db-service",
            trace_id="trace-abc",
            metadata={"duration_ms": 5000},
        )
        d = p.to_dict()
        assert d["service"] == "db-service"
        assert d["traceId"] == "trace-abc"
        assert d["metadata"]["duration_ms"] == 5000

    def test_optional_fields_absent_when_none(self):
        p = LogPayload(level="debug", message="x")
        d = p.to_dict()
        assert "service" not in d
        assert "traceId" not in d
        assert "metadata" not in d


# ---------------------------------------------------------------------------
# HttpRequestItem / HttpRequestBatch
# ---------------------------------------------------------------------------

class TestHttpRequestItem:
    def _item(self, **overrides):
        defaults = dict(
            trace_id="t1",
            direction="outbound",
            method="get",
            host="api.example.com",
            path="/v1/data",
            status_code=200,
            duration_ms=100,
            request_size=0,
            response_size=512,
            timestamp="2026-03-31T12:00:00Z",
        )
        defaults.update(overrides)
        return HttpRequestItem(**defaults)

    def test_basic(self):
        d = self._item().to_dict()
        assert d["traceId"] == "t1"
        assert d["direction"] == "outbound"
        assert d["method"] == "GET"  # uppercased
        assert d["statusCode"] == 200
        assert d["durationMs"] == 100

    def test_invalid_direction(self):
        with pytest.raises(ValueError, match="direction"):
            self._item(direction="sideways").to_dict()

    def test_negative_duration_raises(self):
        with pytest.raises(ValueError, match="duration_ms"):
            self._item(duration_ms=-1).to_dict()

    def test_optional_fields_absent(self):
        d = self._item().to_dict()
        assert "userId" not in d
        assert "errorFingerprint" not in d


class TestHttpRequestBatch:
    def _item(self):
        return HttpRequestItem(
            trace_id="t",
            direction="inbound",
            method="POST",
            host="h",
            path="/p",
            status_code=201,
            duration_ms=50,
            request_size=100,
            response_size=200,
            timestamp="2026-03-31T00:00:00Z",
        )

    def test_empty_batch_raises(self):
        b = HttpRequestBatch(requests=[])
        with pytest.raises(ValueError, match="at least 1"):
            b.to_dict()

    def test_over_100_raises(self):
        b = HttpRequestBatch(requests=[self._item()] * 101)
        with pytest.raises(ValueError, match="100"):
            b.to_dict()

    def test_100_items_ok(self):
        b = HttpRequestBatch(requests=[self._item()] * 100)
        d = b.to_dict()
        assert len(d["requests"]) == 100


# ---------------------------------------------------------------------------
# ReplayEvent / ReplayPayload
# ---------------------------------------------------------------------------

class TestReplayEvent:
    def test_from_dict_serializes_to_json_string(self):
        e = ReplayEvent.from_dict("click", {"x": 10, "y": 20})
        d = e.to_dict()
        assert d["eventType"] == "click"
        # eventData must be a string
        assert isinstance(d["eventData"], str)
        parsed = json.loads(d["eventData"])
        assert parsed == {"x": 10, "y": 20}

    def test_raw_string_eventdata(self):
        e = ReplayEvent(event_type="nav", event_data='{"url":"/home"}')
        d = e.to_dict()
        assert d["eventData"] == '{"url":"/home"}'

    def test_optional_url_and_ts(self):
        e = ReplayEvent.from_dict("scroll", {"y": 400}, url="http://x.com", timestamp_millis=123)
        d = e.to_dict()
        assert d["url"] == "http://x.com"
        assert d["timestampMillis"] == 123


class TestReplayPayload:
    def test_empty_events_raises(self):
        p = ReplayPayload(fingerprint="fp", session_id="sid", events=[])
        with pytest.raises(ValueError, match="at least 1"):
            p.to_dict()

    def test_events_sorted_by_timestamp(self):
        e1 = ReplayEvent.from_dict("click", {}, timestamp_millis=200)
        e2 = ReplayEvent.from_dict("scroll", {}, timestamp_millis=100)
        p = ReplayPayload(fingerprint="fp", session_id="sid", events=[e1, e2])
        d = p.to_dict()
        ts = [ev["timestampMillis"] for ev in d["events"]]
        assert ts == sorted(ts)

    def test_required_fields(self):
        e = ReplayEvent.from_dict("nav", {})
        p = ReplayPayload(fingerprint="fp-1", session_id="sid-1", events=[e])
        d = p.to_dict()
        assert d["fingerprint"] == "fp-1"
        assert d["sessionId"] == "sid-1"
        assert len(d["events"]) == 1


# ---------------------------------------------------------------------------
# HeartbeatPayload
# ---------------------------------------------------------------------------

class TestHeartbeatPayload:
    def test_valid_success(self):
        p = HeartbeatPayload(slug="my-job", status="success", duration_ms=1000)
        d = p.to_dict()
        assert d == {"slug": "my-job", "status": "success", "durationMs": 1000}

    def test_valid_failed(self):
        p = HeartbeatPayload(slug="my-job", status="FAILED", duration_ms=500, message="oops")
        d = p.to_dict()
        assert d["status"] == "failed"  # lowercased
        assert d["message"] == "oops"

    def test_invalid_slug(self):
        p = HeartbeatPayload(slug="My Job!", status="success", duration_ms=0)
        with pytest.raises(ValueError, match="slug"):
            p.to_dict()

    def test_invalid_status(self):
        p = HeartbeatPayload(slug="job", status="ok", duration_ms=0)
        with pytest.raises(ValueError, match="status"):
            p.to_dict()

    def test_message_absent_when_none(self):
        p = HeartbeatPayload(slug="j", status="success", duration_ms=0, message=None)
        d = p.to_dict()
        assert "message" not in d
