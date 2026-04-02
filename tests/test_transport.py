"""Unit tests for the HTTP transport layer (mocked network)."""

import pytest
import respx
import httpx
from unittest.mock import patch

from allstak.transport import HttpTransport, AllStakAuthError, AllStakTransportError


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

    @respx.mock
    def test_returns_status_and_body(self):
        respx.post(f"{BASE}/ingest/v1/logs").mock(
            return_value=httpx.Response(202, json={"success": True, "data": {"id": "x"}})
        )
        t = make_transport()
        status, body = t.post("/ingest/v1/logs", {"level": "info", "message": "hi"})
        assert status == 202
        assert body["data"]["id"] == "x"


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
