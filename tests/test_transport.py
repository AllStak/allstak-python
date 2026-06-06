"""Unit tests for the HTTP transport layer (mocked network)."""

import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest
import respx
import httpx
from unittest.mock import patch

from allstak.transport import (
    HttpTransport,
    AllStakAuthError,
    AllStakTransportError,
    parse_retry_after,
)


BASE = "http://test-backend:8080"
KEY = "ask_test_key"


def make_transport(**kwargs) -> HttpTransport:
    defaults = dict(api_key=KEY, host=BASE, connect_timeout=1.0, read_timeout=1.0, max_retries=3)
    defaults.update(kwargs)
    return HttpTransport(**defaults)


class TestTransportHeaders:
    @respx.mock
    def test_sends_api_key_header(self):
        route = respx.post(f"{BASE}/ingest/v1/errors").mock(
            return_value=httpx.Response(202, json={"success": True, "data": {"id": "abc"}})
        )
        t = make_transport()
        t.post("/ingest/v1/errors", {"exceptionClass": "E", "message": "m"})
        req = route.calls[0].request
        assert req.headers["x-allstak-key"] == KEY
        assert req.headers["content-type"] == "application/json"
        stats = t.stats()
        assert stats["eventsCaptured"] == 1
        assert stats["eventsSent"] == 1
        assert stats["eventsFailed"] == 0

    @respx.mock
    def test_returns_status_and_body(self):
        respx.post(f"{BASE}/ingest/v1/logs").mock(
            return_value=httpx.Response(202, json={"success": True, "data": {"id": "x"}})
        )
        t = make_transport()
        status, body = t.post("/ingest/v1/logs", {"level": "info", "message": "hi"})
        assert status == 202
        assert body["data"]["id"] == "x"


class TestTransportCompression:
    @respx.mock
    def test_tiny_payload_is_not_compressed(self):
        route = respx.post(f"{BASE}/ingest/v1/logs").mock(
            return_value=httpx.Response(202, json={"success": True})
        )
        t = make_transport()
        t.post("/ingest/v1/logs", {"level": "info", "message": "hi"})

        req = route.calls[0].request
        assert "content-encoding" not in req.headers
        assert json.loads(req.content.decode("utf-8"))["message"] == "hi"
        stats = t.stats()
        assert stats["uncompressedPayloads"] == 1
        assert stats["compressedPayloads"] == 0
        assert stats["compressionBytesSaved"] == 0

    @respx.mock
    def test_large_payload_is_gzipped_when_smaller(self):
        route = respx.post(f"{BASE}/ingest/v1/errors").mock(
            return_value=httpx.Response(202, json={"success": True})
        )
        t = make_transport()
        message = "x" * 8000
        t.post("/ingest/v1/errors", {"exceptionClass": "E", "message": message})

        req = route.calls[0].request
        assert req.headers["content-encoding"] == "gzip"
        decoded = json.loads(gzip.decompress(req.content).decode("utf-8"))
        assert decoded["message"] == message
        stats = t.stats()
        assert stats["compressedPayloads"] == 1
        assert stats["uncompressedPayloads"] == 0
        assert stats["compressionBytesSaved"] > 0


class TestTransport401:
    @respx.mock
    def test_401_raises_auth_error(self):
        respx.post(f"{BASE}/ingest/v1/errors").mock(
            return_value=httpx.Response(
                401,
                json={"success": False, "error": {"code": "INVALID_API_KEY", "message": "bad key"}},
            )
        )
        t = make_transport()
        with pytest.raises(AllStakAuthError):
            t.post("/ingest/v1/errors", {})

    @respx.mock
    def test_401_disables_transport(self):
        respx.post(f"{BASE}/ingest/v1/errors").mock(
            return_value=httpx.Response(401, json={})
        )
        t = make_transport()
        try:
            t.post("/ingest/v1/errors", {})
        except AllStakAuthError:
            pass
        assert t.is_disabled()

    def test_disabled_transport_raises_immediately(self):
        t = make_transport()
        t._disabled = True
        with pytest.raises(AllStakAuthError):
            t.post("/ingest/v1/errors", {})


class TestTransportRetry:
    @respx.mock
    def test_5xx_retried(self):
        """Should retry on 5xx up to max_retries."""
        route = respx.post(f"{BASE}/ingest/v1/errors").mock(
            return_value=httpx.Response(500, json={"error": "server error"})
        )
        t = make_transport(max_retries=3)
        with patch("time.sleep"):  # skip actual sleep
            with pytest.raises(AllStakTransportError):
                t.post("/ingest/v1/errors", {})
        assert route.call_count == 3
        stats = t.stats()
        assert stats["eventsCaptured"] == 1
        assert stats["eventsFailed"] == 1
        assert stats["eventsDropped"] == 1
        assert stats["retryAttempts"] == 2

    @respx.mock
    def test_422_not_retried(self):
        """Should NOT retry on 422 (client validation error)."""
        route = respx.post(f"{BASE}/ingest/v1/errors").mock(
            return_value=httpx.Response(422, json={"error": "validation"})
        )
        t = make_transport(max_retries=3)
        with patch("time.sleep"):
            status, _ = t.post("/ingest/v1/errors", {})
        assert status == 422
        assert route.call_count == 1
        stats = t.stats()
        assert stats["eventsFailed"] == 1
        assert stats["eventsDropped"] == 1

    @respx.mock
    def test_402_feature_gate_not_retried(self):
        """Feature-gated telemetry is terminal, not an offline-retry wedge."""
        route = respx.post(f"{BASE}/ingest/v1/spans").mock(
            return_value=httpx.Response(
                402,
                json={"error": {"code": "FEATURE_NOT_AVAILABLE"}},
            )
        )
        t = make_transport(max_retries=3)
        with patch("time.sleep"):
            status, _ = t.post("/ingest/v1/spans", {"spans": []})
        assert status == 402
        assert route.call_count == 1
        stats = t.stats()
        assert stats["eventsFailed"] == 1
        assert stats["eventsDropped"] == 1

    @respx.mock
    def test_400_not_retried(self):
        route = respx.post(f"{BASE}/ingest/v1/logs").mock(
            return_value=httpx.Response(400, json={"error": "bad request"})
        )
        t = make_transport(max_retries=3)
        with patch("time.sleep"):
            status, _ = t.post("/ingest/v1/logs", {})
        assert status == 400
        assert route.call_count == 1

    @respx.mock
    def test_network_timeout_retried(self):
        """Connection timeout should trigger retry."""
        route = respx.post(f"{BASE}/ingest/v1/errors").mock(
            side_effect=httpx.ConnectTimeout("timeout")
        )
        t = make_transport(max_retries=3)
        with patch("time.sleep"):
            with pytest.raises(AllStakTransportError):
                t.post("/ingest/v1/errors", {})
        assert route.call_count == 3

    @respx.mock
    def test_succeeds_on_second_attempt(self):
        """Should succeed if server recovers after one 5xx."""
        call_count = 0

        def side_effect(request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(500, json={})
            return httpx.Response(202, json={"success": True, "data": {"id": "ok"}})

        respx.post(f"{BASE}/ingest/v1/errors").mock(side_effect=side_effect)
        t = make_transport(max_retries=3)
        with patch("time.sleep"):
            status, body = t.post("/ingest/v1/errors", {})
        assert status == 202
        assert call_count == 2
        stats = t.stats()
        assert stats["eventsSent"] == 1
        assert stats["retryAttempts"] == 1


class TestParseRetryAfter:
    """Pure-function tests for the Retry-After parser (no real sleeping)."""

    def test_integer_seconds(self):
        assert parse_retry_after("2") == 2.0

    def test_http_date_delta(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        future = now + timedelta(seconds=42)
        header = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert parse_retry_after(header, now=now) == pytest.approx(42.0, abs=1.0)

    def test_http_date_in_past_is_zero(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        past = now - timedelta(seconds=60)
        header = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert parse_retry_after(header, now=now) == 0.0

    def test_none_is_zero(self):
        assert parse_retry_after(None) == 0.0

    def test_empty_is_zero(self):
        assert parse_retry_after("") == 0.0
        assert parse_retry_after("   ") == 0.0

    def test_garbage_is_zero(self):
        assert parse_retry_after("soon") == 0.0
        assert parse_retry_after("12abc") == 0.0

    def test_negative_is_zero(self):
        assert parse_retry_after("-5") == 0.0

    def test_clamped_to_300(self):
        assert parse_retry_after("999") == 300.0
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        far = now + timedelta(seconds=10000)
        header = far.strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert parse_retry_after(header, now=now) == 300.0


class TestTransport429:
    @respx.mock
    def test_429_is_retried_not_dropped(self):
        """A 429 must trigger retries, not be silently dropped."""
        route = respx.post(f"{BASE}/ingest/v1/errors").mock(
            return_value=httpx.Response(
                429, headers={"Retry-After": "1"}, json={"error": "rate limited"}
            )
        )
        t = make_transport(max_retries=3)
        with patch("time.sleep") as sleep_mock:
            with pytest.raises(AllStakTransportError):
                t.post("/ingest/v1/errors", {})
        # Retried up to max_retries (not returned/dropped after attempt 1).
        assert route.call_count == 3
        # Honored the Retry-After header value (1s, no jitter).
        sleep_mock.assert_called_with(1.0)
        stats = t.stats()
        assert stats["rateLimitedCount"] == 3
        assert stats["retryAttempts"] == 2
        assert stats["eventsFailed"] == 1

    @respx.mock
    def test_429_recovers_on_retry(self):
        """A 429 followed by a 202 should succeed (event not lost)."""
        call_count = 0

        def side_effect(request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(429, headers={"Retry-After": "2"}, json={})
            return httpx.Response(202, json={"success": True, "data": {"id": "ok"}})

        route = respx.post(f"{BASE}/ingest/v1/errors").mock(side_effect=side_effect)
        t = make_transport(max_retries=3)
        with patch("time.sleep") as sleep_mock:
            status, body = t.post("/ingest/v1/errors", {})
        assert status == 202
        assert call_count == 2
        sleep_mock.assert_called_with(2.0)
        stats = t.stats()
        assert stats["rateLimitedCount"] == 1
        assert stats["eventsSent"] == 1

    @respx.mock
    def test_429_without_retry_after_falls_back_to_backoff(self):
        """Absent Retry-After → exponential backoff (1s + jitter on attempt 1)."""
        route = respx.post(f"{BASE}/ingest/v1/errors").mock(
            return_value=httpx.Response(429, json={})
        )
        t = make_transport(max_retries=2)
        with patch("time.sleep") as sleep_mock:
            with pytest.raises(AllStakTransportError):
                t.post("/ingest/v1/errors", {})
        assert route.call_count == 2
        # First (only) backoff uses base 1.0s plus jitter in [0, 0.5).
        slept = sleep_mock.call_args[0][0]
        assert 1.0 <= slept < 1.5
