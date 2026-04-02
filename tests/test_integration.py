"""
Integration tests — sends REAL requests to the local AllStak backend.

Requires the backend to be running at http://localhost:8080.
Skip with: pytest -m "not integration"

These tests validate that:
1. The SDK constructs valid payloads accepted by the real backend
2. The backend returns the expected status codes
3. No crashes occur under error conditions
"""

import time
import uuid
import pytest

from allstak.config import AllStakConfig
from allstak.transport import HttpTransport, AllStakAuthError
from allstak.models.errors import ErrorPayload, UserContext
from allstak.models.logs import LogPayload
from allstak.models.http_requests import HttpRequestItem, HttpRequestBatch
from allstak.models.replay import ReplayEvent, ReplayPayload
from allstak.models.heartbeat import HeartbeatPayload
from allstak.client import AllStakClient

pytestmark = pytest.mark.integration

REAL_HOST = "http://localhost:8080"
REAL_API_KEY = "ask_live_o5fmoedqr14vxm47rltn9frjpazjszh7"
BAD_API_KEY = "ask_bad_key_does_not_exist"


def make_config(api_key=REAL_API_KEY) -> AllStakConfig:
    return AllStakConfig(
        api_key=api_key,
        host=REAL_HOST,
        environment="test",
        release="0.1.0-test",
    )


def make_transport(api_key=REAL_API_KEY) -> HttpTransport:
    return HttpTransport(
        api_key=api_key,
        host=REAL_HOST,
        connect_timeout=3.0,
        read_timeout=3.0,
        max_retries=2,
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class TestAuth:
    def test_valid_api_key_accepted(self):
        t = make_transport()
        status, body = t.post("/ingest/v1/logs", {"level": "info", "message": "auth test"})
        assert status == 202

    def test_invalid_api_key_raises_auth_error(self):
        t = make_transport(api_key=BAD_API_KEY)
        with pytest.raises(AllStakAuthError):
            t.post("/ingest/v1/errors", {"exceptionClass": "E", "message": "m"})

    def test_invalid_key_disables_transport(self):
        t = make_transport(api_key=BAD_API_KEY)
        try:
            t.post("/ingest/v1/errors", {"exceptionClass": "E", "message": "m"})
        except AllStakAuthError:
            pass
        assert t.is_disabled()


# ---------------------------------------------------------------------------
# Error ingestion
# ---------------------------------------------------------------------------

class TestErrorIngestion:
    def test_minimal_error(self):
        t = make_transport()
        status, body = t.post("/ingest/v1/errors", {
            "exceptionClass": "IntegrationTestError",
            "message": "minimal test error from Python SDK",
        })
        assert status == 202
        assert body.get("success") is True
        assert "id" in body.get("data", {})

    def test_full_error_payload(self):
        t = make_transport()
        payload = ErrorPayload(
            exception_class="DatabaseConnectionError",
            message="Could not connect to postgres",
            stack_trace=[
                "File 'db.py', line 42, in connect",
                "  raise DatabaseConnectionError(msg)",
            ],
            level="fatal",
            environment="test",
            release="0.1.0",
            session_id=str(uuid.uuid4()),
            user=UserContext(id="usr-001", email="test@allstak.dev"),
            metadata={"host": "localhost", "port": 5432},
        )
        status, body = t.post("/ingest/v1/errors", payload.to_dict())
        assert status == 202

    def test_capture_exception_returns_event_id(self):
        config = make_config()
        transport = make_transport()
        from allstak.modules.errors import ErrorModule
        module = ErrorModule(transport, config)
        try:
            raise ValueError("integration test exception")
        except ValueError as e:
            event_id = module.capture_exception(e, metadata={"source": "pytest"})
        assert event_id is not None
        assert len(event_id) > 0

    def test_validation_error_returns_422(self):
        """Missing required field should return 422 (not 400 as guidelines say)."""
        t = make_transport()
        status, body = t.post("/ingest/v1/errors", {"message": "missing exceptionClass"})
        # Backend returns 422 for validation failures (guideline says 400 — documented mismatch)
        assert status == 422
        assert body.get("error", {}).get("code") == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Log ingestion
# ---------------------------------------------------------------------------

class TestLogIngestion:
    def test_info_log(self):
        t = make_transport()
        payload = LogPayload(level="info", message="Python SDK integration test log")
        status, body = t.post("/ingest/v1/logs", payload.to_dict())
        assert status == 202

    def test_all_log_levels(self):
        t = make_transport()
        for level in ("debug", "info", "warn", "error", "fatal"):
            payload = LogPayload(level=level, message=f"Test log at level {level}")
            status, _ = t.post("/ingest/v1/logs", payload.to_dict())
            assert status == 202, f"Level {level} returned {status}"

    def test_log_with_metadata(self):
        t = make_transport()
        payload = LogPayload(
            level="warn",
            message="Slow database query detected",
            service="db-service",
            trace_id="trace-py-001",
            metadata={"query_ms": 4500, "table": "orders"},
        )
        status, _ = t.post("/ingest/v1/logs", payload.to_dict())
        assert status == 202

    def test_log_module_flush(self):
        """Test the LogModule buffer+flush flow."""
        config = make_config()
        transport = make_transport()
        from allstak.modules.logs import LogModule
        module = LogModule(transport, config)
        module.info("Buffer flush integration test", service="pytest")
        module.warn("Another buffered log", metadata={"run": "integration"})
        time.sleep(0.1)
        module.flush()
        time.sleep(0.2)
        module.shutdown()
        # If no exception is raised, the test passes


# ---------------------------------------------------------------------------
# HTTP request monitoring
# ---------------------------------------------------------------------------

class TestHttpMonitoring:
    def test_single_request(self):
        t = make_transport()
        item = HttpRequestItem(
            trace_id=str(uuid.uuid4()),
            direction="outbound",
            method="GET",
            host="api.example.com",
            path="/v1/data",
            status_code=200,
            duration_ms=142,
            request_size=0,
            response_size=512,
            timestamp="2026-03-31T12:00:00Z",
        )
        batch = HttpRequestBatch(requests=[item])
        status, body = t.post("/ingest/v1/http-requests", batch.to_dict())
        assert status == 202
        assert body.get("ok") is True

    def test_batch_of_5(self):
        t = make_transport()
        items = []
        for i in range(5):
            items.append(HttpRequestItem(
                trace_id=str(uuid.uuid4()),
                direction="inbound",
                method="POST",
                host="myapp.com",
                path=f"/api/endpoint{i}",
                status_code=200 + i,
                duration_ms=i * 10,
                request_size=100,
                response_size=200,
                timestamp="2026-03-31T12:00:00Z",
            ))
        batch = HttpRequestBatch(requests=items)
        status, body = t.post("/ingest/v1/http-requests", batch.to_dict())
        assert status == 202
        assert body.get("accepted") == 5

    def test_http_module_record(self):
        config = make_config()
        transport = make_transport()
        from allstak.modules.http_monitor import HttpMonitorModule
        module = HttpMonitorModule(transport, config)
        module.record(
            direction="outbound",
            method="POST",
            host="payments.stripe.com",
            path="/v1/charges",
            status_code=200,
            duration_ms=320,
            request_size=256,
            response_size=1024,
        )
        time.sleep(0.1)
        module.flush()
        time.sleep(0.2)
        module.shutdown()


# ---------------------------------------------------------------------------
# Session replay ingestion
# ---------------------------------------------------------------------------

class TestReplayIngestion:
    def test_single_event(self):
        t = make_transport()
        session_id = str(uuid.uuid4())
        payload = ReplayPayload(
            fingerprint=session_id,
            session_id=session_id,
            events=[
                ReplayEvent.from_dict(
                    "navigation",
                    {"from": "/home", "to": "/checkout"},
                    url="http://localhost:3000/checkout",
                    timestamp_millis=int(time.time() * 1000),
                )
            ],
        )
        status, body = t.post("/ingest/v1/replay", payload.to_dict())
        assert status == 202
        assert body.get("data", {}).get("eventsReceived") == 1

    def test_multiple_events_sorted(self):
        t = make_transport()
        sid = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        payload = ReplayPayload(
            fingerprint=sid,
            session_id=sid,
            events=[
                ReplayEvent.from_dict("click", {"target": "button#checkout"}, timestamp_millis=now_ms + 200),
                ReplayEvent.from_dict("scroll", {"scrollY": 400}, timestamp_millis=now_ms + 100),
                ReplayEvent.from_dict("navigation", {"url": "/cart"}, timestamp_millis=now_ms),
            ],
        )
        d = payload.to_dict()
        ts = [e["timestampMillis"] for e in d["events"]]
        assert ts == sorted(ts), "Events not sorted by timestamp"
        status, _ = t.post("/ingest/v1/replay", d)
        assert status == 202


# ---------------------------------------------------------------------------
# Cron heartbeat
# ---------------------------------------------------------------------------

class TestCronHeartbeat:
    def test_unknown_slug_returns_404(self):
        """Backend returns 404 for slug not registered in console."""
        t = make_transport()
        payload = HeartbeatPayload(slug="nonexistent-job-slug", status="success", duration_ms=1000)
        status, body = t.post("/ingest/v1/heartbeat", payload.to_dict())
        assert status == 404

    def test_cron_module_unknown_slug(self):
        config = make_config()
        transport = make_transport()
        from allstak.modules.cron import CronModule
        module = CronModule(transport, config)
        result = module.ping("totally-unknown-slug", "success", 1000)
        assert result is False  # 404 handled gracefully


# ---------------------------------------------------------------------------
# AllStakClient integration
# ---------------------------------------------------------------------------

class TestAllStakClient:
    def test_client_capture_exception(self):
        config = make_config()
        client = AllStakClient(config)
        try:
            raise RuntimeError("Full client integration test")
        except RuntimeError as e:
            event_id = client.capture_exception(e, metadata={"test": True})
        assert event_id is not None
        client.flush()

    def test_client_fail_safe_on_auth_error(self):
        """Client should disable itself gracefully on 401, not raise."""
        bad_config = make_config(api_key=BAD_API_KEY)
        client = AllStakClient(bad_config)
        # Should not raise — should return None after disabling
        result = client.capture_exception(ValueError("test"))
        # After auth error, client is disabled
        assert client._disabled

    def test_client_does_not_crash_on_network_error(self):
        """SDK must fail silently on unreachable host."""
        config = AllStakConfig(api_key="key", host="http://localhost:19999")
        client = AllStakClient(config)
        # Should not raise
        result = client.capture_exception(ValueError("unreachable host test"))
        assert result is None
