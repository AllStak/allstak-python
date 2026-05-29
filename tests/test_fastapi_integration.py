"""Standalone behaviour tests for the FastAPI / Starlette ASGI integration.

These drive a *real* FastAPI app through ``starlette.testclient.TestClient``
(httpx-backed) wrapped by ``AllStakFastAPI`` / ``AllStakASGIMiddleware`` and
assert what the integration *reports* — by spying on the public client
surface (``capture_exception`` / ``capture_error`` / ``http.record`` /
``set_user`` / ``start_span``). No network is ever touched.

Parity focus:

* Status-code-driven capture: 5xx reported (raised OR ``HTTPException(5xx)``),
  4xx never reported by default.
* Unhandled errors in sync AND async handlers are captured and re-raised.
* Span opens + finishes per request with a status.
* The span/transaction is named by the route TEMPLATE (``/items/{item_id}``),
  not the concrete path (``/items/42``).
* Streaming responses keep the middleware intact and the span finishes after
  the body completes.
* User context from ``request.state.user``.
* Fail-open: an observability error must never break the host request.
* OPTIONS / HEAD are excluded from transactions by default.
* Background-task errors that surface through ASGI are reported.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from starlette.responses import StreamingResponse
from starlette.testclient import TestClient

import allstak
from allstak import client as allstak_client
from allstak.integrations.fastapi import (
    DEFAULT_HTTP_METHODS_TO_CAPTURE,
    AllStakASGIMiddleware,
    AllStakFastAPI,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def spied_client():
    """Init the SDK and replace the reporting surface with spies.

    Spies are attached to the live singleton client so the middleware's
    ``client.capture_exception(...)`` / ``client.http.record(...)`` etc. calls
    are observed without any transport / network activity.
    """
    allstak_client._client = None
    allstak_client._initialized_once = False
    allstak.init(api_key="ask_test", host="http://allstak.test")

    client = allstak.get_client()
    assert client is not None, "init() should have created a singleton client"

    spies = {
        "capture_exception": MagicMock(return_value="evt-exc"),
        "capture_error": MagicMock(return_value="evt-err"),
        "set_user": MagicMock(return_value=None),
        "http_record": MagicMock(return_value=None),
    }
    client.capture_exception = spies["capture_exception"]
    client.capture_error = spies["capture_error"]
    client.set_user = spies["set_user"]
    client.http.record = spies["http_record"]

    # Wrap the real span factory so the produced span object is itself a spy
    # we can assert opened + finished, while keeping real trace context.
    #
    # NOTE: the SDK also auto-instruments the outbound httpx client the
    # TestClient uses, producing "http.client" spans. We only collect the
    # inbound "http.server" request spans created by THIS middleware so the
    # assertions describe the integration under test, not the test transport.
    real_start_span = client.start_span
    created_spans = []

    def _spy_start_span(operation, *, description="", tags=None):
        span = real_start_span(operation, description=description, tags=tags)
        if operation == "http.server":
            finish_spy = MagicMock(wraps=span.finish)
            span.finish = finish_spy
            created_spans.append(span)
        return span

    client.start_span = _spy_start_span
    spies["spans"] = created_spans

    # The SDK also auto-instruments the outbound httpx transport the TestClient
    # uses, so http.record gets BOTH the middleware's inbound call and httpx's
    # outbound call. This helper isolates the inbound records (what THIS
    # middleware emits) for assertions.
    def _inbound_records():
        return [
            c
            for c in spies["http_record"].call_args_list
            if c.kwargs.get("direction") == "inbound"
        ]

    spies["inbound_records"] = _inbound_records

    yield spies

    allstak_client._client = None
    allstak_client._initialized_once = False


def _build_app() -> FastAPI:
    """A real FastAPI app exercising every route shape under test."""
    app = FastAPI()

    @app.get("/sync")
    def sync_route():
        return {"kind": "sync"}

    @app.get("/async")
    async def async_route():
        await asyncio.sleep(0)
        return {"kind": "async"}

    @app.get("/sync-raise")
    def sync_raise():
        raise ValueError("sync boom")

    @app.get("/async-raise")
    async def async_raise():
        await asyncio.sleep(0)
        raise RuntimeError("async boom")

    @app.get("/server-error")
    def server_error():
        # A framework HTTPException with a 5xx code — handled by the framework's
        # exception handler, so it is NOT raised out as an exception.
        raise HTTPException(status_code=503, detail="service down")

    @app.get("/not-found")
    def not_found():
        raise HTTPException(status_code=404, detail="missing")

    @app.get("/items/{item_id}")
    def get_item(item_id: int):
        return {"id": item_id}

    @app.get("/items/{item_id}/raise")
    def get_item_raise(item_id: int):
        raise ValueError(f"item {item_id} blew up")

    @app.get("/stream")
    def stream():
        def gen():
            for i in range(5):
                yield f"chunk-{i};".encode()

        return StreamingResponse(gen(), media_type="text/plain")

    @app.get("/me")
    def me(request: Request):
        request.state.user = {"id": "user-42", "email": "user42@example.com"}
        return {"ok": True}

    @app.get("/me-object")
    def me_object(request: Request):
        class _User:
            user_id = "obj-7"
            email = "obj7@example.com"

        request.state.user = _User()
        return {"ok": True}

    @app.get("/bg-task")
    def bg_task(background_tasks: BackgroundTasks):
        def work():
            # Background work that succeeds — must not break anything.
            return None

        background_tasks.add_task(work)
        return {"scheduled": True}

    return app


@pytest.fixture
def app():
    return _build_app()


@pytest.fixture
def client(app, spied_client):
    AllStakFastAPI(app, service="test-api")
    # raise_server_exceptions=False so the TestClient returns the 500 response
    # the way a real ASGI server would, instead of re-raising into the test.
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Happy-path: spans open + finish, telemetry recorded
# ---------------------------------------------------------------------------
def test_sync_route_records_span_and_telemetry(client, spied_client):
    resp = client.get("/sync")
    assert resp.status_code == 200
    assert resp.json() == {"kind": "sync"}

    # One span opened for the request and it finished with "ok".
    assert len(spied_client["spans"]) == 1
    span = spied_client["spans"][0]
    span.finish.assert_called_once()
    assert span.finish.call_args.args[0] == "ok"

    # HTTP telemetry recorded for the inbound request.
    inbound = spied_client["inbound_records"]()
    assert len(inbound) == 1
    kw = inbound[0].kwargs
    assert kw["method"] == "GET"
    assert kw["status_code"] == 200

    # No error reported on a 2xx.
    spied_client["capture_exception"].assert_not_called()
    spied_client["capture_error"].assert_not_called()


def test_async_route_records_span_and_telemetry(client, spied_client):
    resp = client.get("/async")
    assert resp.status_code == 200
    assert resp.json() == {"kind": "async"}

    assert len(spied_client["spans"]) == 1
    spied_client["spans"][0].finish.assert_called_once_with("ok")
    assert len(spied_client["inbound_records"]()) == 1
    spied_client["capture_exception"].assert_not_called()


# ---------------------------------------------------------------------------
# Unhandled errors in sync AND async handlers: captured + re-raised
# ---------------------------------------------------------------------------
def test_sync_unhandled_error_is_captured_and_reraised(app, spied_client):
    AllStakFastAPI(app, service="test-api")

    # With raise_server_exceptions=True the exception must propagate out of the
    # middleware (proving it re-raises rather than swallowing).
    strict = TestClient(app, raise_server_exceptions=True)
    with pytest.raises(ValueError, match="sync boom"):
        strict.get("/sync-raise")

    spied_client["capture_exception"].assert_called_once()
    call = spied_client["capture_exception"].call_args
    captured_exc = call.args[0]
    assert isinstance(captured_exc, ValueError)
    meta = call.kwargs["metadata"]
    assert meta["http.method"] == "GET"
    assert meta["http.path"] == "/sync-raise"
    assert meta["http.status"] == 500
    # Marked handled=True with the integration mechanism type.
    assert call.kwargs["mechanism"]["handled"] is True
    assert call.kwargs["mechanism"]["type"] == "fastapi"
    # Request context carries method/status.
    req_ctx = call.kwargs["request_context"]
    assert req_ctx.method == "GET"
    assert req_ctx.status_code == 500


def test_async_unhandled_error_is_captured_and_reraised(app, spied_client):
    AllStakFastAPI(app, service="test-api")
    strict = TestClient(app, raise_server_exceptions=True)
    with pytest.raises(RuntimeError, match="async boom"):
        strict.get("/async-raise")

    spied_client["capture_exception"].assert_called_once()
    captured_exc = spied_client["capture_exception"].call_args.args[0]
    assert isinstance(captured_exc, RuntimeError)


def test_unhandled_error_returns_500_to_client(client, spied_client):
    # Non-strict client: the framework turns the unhandled error into a 500.
    resp = client.get("/sync-raise")
    assert resp.status_code == 500
    spied_client["capture_exception"].assert_called_once()

    # Span still finished, with an error status.
    assert len(spied_client["spans"]) == 1
    spied_client["spans"][0].finish.assert_called_once_with("error")


# ---------------------------------------------------------------------------
# Status-code driven capture: 5xx reported, 4xx ignored
# ---------------------------------------------------------------------------
def test_http_exception_5xx_is_reported(client, spied_client):
    resp = client.get("/server-error")
    assert resp.status_code == 503

    # No exception bubbled out (the framework handled it), so it is reported
    # via capture_error, NOT capture_exception.
    spied_client["capture_exception"].assert_not_called()
    spied_client["capture_error"].assert_called_once()
    call = spied_client["capture_error"].call_args
    assert call.kwargs["metadata"]["http.status"] == 503

    # Span finished with error status for the 5xx.
    assert len(spied_client["spans"]) == 1
    spied_client["spans"][0].finish.assert_called_once_with("error")


def test_http_exception_4xx_is_not_reported(client, spied_client):
    resp = client.get("/not-found")
    assert resp.status_code == 404

    # 4xx client errors are deliberately NOT reported.
    spied_client["capture_exception"].assert_not_called()
    spied_client["capture_error"].assert_not_called()

    # Telemetry is still recorded for the 404.
    inbound = spied_client["inbound_records"]()
    assert len(inbound) == 1
    assert inbound[0].kwargs["status_code"] == 404

    # Span finished with "ok" — a 4xx is not a server error.
    assert len(spied_client["spans"]) == 1
    spied_client["spans"][0].finish.assert_called_once_with("ok")


def test_unmatched_route_404_is_not_reported(client, spied_client):
    # A path with no matching route → framework 404, generic fallback name.
    resp = client.get("/definitely-not-a-route")
    assert resp.status_code == 404
    spied_client["capture_exception"].assert_not_called()
    spied_client["capture_error"].assert_not_called()


def test_failed_request_status_codes_is_configurable(app, spied_client):
    # Opt in to reporting 404s as well.
    app.add_middleware(
        AllStakASGIMiddleware,
        service="test-api",
        failed_request_status_codes={404, *range(500, 600)},
    )
    cl = TestClient(app, raise_server_exceptions=False)
    resp = cl.get("/not-found")
    assert resp.status_code == 404
    # Now the 404 is reported (as a server-side capture_error, no exception).
    spied_client["capture_error"].assert_called_once()
    assert spied_client["capture_error"].call_args.kwargs["metadata"]["http.status"] == 404


# ---------------------------------------------------------------------------
# Route-template (low cardinality) naming
# ---------------------------------------------------------------------------
def test_span_named_by_route_template_not_concrete_path(client, spied_client):
    resp = client.get("/items/42")
    assert resp.status_code == 200
    assert resp.json() == {"id": 42}

    span = spied_client["spans"][0]
    span.finish.assert_called_once_with("ok")
    tags = span.to_dict().get("tags", {})
    # Templated route, NOT the concrete /items/42.
    assert tags.get("http.route") == "/items/{item_id}"
    assert tags.get("transaction") == "/items/{item_id}"
    assert "42" not in tags.get("http.route", "")


def test_error_on_templated_route_reports_template_path(client, spied_client):
    resp = client.get("/items/77/raise")
    assert resp.status_code == 500

    call = spied_client["capture_exception"].call_args
    # request_context.path and metadata http.route use the template, not "77".
    assert call.kwargs["request_context"].path == "/items/{item_id}/raise"
    assert call.kwargs["metadata"]["http.route"] == "/items/{item_id}/raise"
    # The concrete path is still preserved as http.path for debugging.
    assert call.kwargs["metadata"]["http.path"] == "/items/77/raise"


def test_endpoint_transaction_style_uses_handler_name(app, spied_client):
    app.add_middleware(
        AllStakASGIMiddleware,
        service="test-api",
        transaction_style="endpoint",
    )
    cl = TestClient(app, raise_server_exceptions=False)
    spans_before = len(spied_client["spans"])
    resp = cl.get("/items/9")
    assert resp.status_code == 200
    span = spied_client["spans"][spans_before]
    tags = span.to_dict().get("tags", {})
    assert tags.get("transaction") == "get_item"


# ---------------------------------------------------------------------------
# Streaming responses
# ---------------------------------------------------------------------------
def test_streaming_response_does_not_break_middleware(client, spied_client):
    resp = client.get("/stream")
    assert resp.status_code == 200
    assert resp.text == "chunk-0;chunk-1;chunk-2;chunk-3;chunk-4;"

    # Span finished after the full body completed (single finish, status ok).
    assert len(spied_client["spans"]) == 1
    spied_client["spans"][0].finish.assert_called_once_with("ok")

    # Telemetry recorded a non-zero response size — proving body was observed.
    inbound = spied_client["inbound_records"]()
    assert len(inbound) == 1
    kw = inbound[0].kwargs
    assert kw["status_code"] == 200
    assert kw["response_size"] > 0


@pytest.mark.asyncio
async def test_streaming_span_finishes_after_body_completes(spied_client):
    # TestClient buffers streaming bodies eagerly, so it cannot observe the span
    # state BETWEEN chunks. To prove the span finishes ONLY after the final body
    # chunk (more_body=False) — not when the first chunk is emitted — wrap a
    # minimal raw ASGI streaming app directly with the middleware. A bare ASGI
    # callable avoids the anyio task-group machinery a full FastAPI app needs.
    async def streaming_app(scope, receive, send):
        assert scope["type"] == "http"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        for i in range(3):
            await send(
                {
                    "type": "http.response.body",
                    "body": f"chunk-{i};".encode(),
                    "more_body": True,
                }
            )
        # Final empty chunk closes the response.
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    middleware = AllStakASGIMiddleware(streaming_app, service="test-api")

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/stream",
        "raw_path": b"/stream",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "server": ("testserver", 80),
        "scheme": "http",
        "client": ("testclient", 50000),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    observed = []  # (more_body, span.finish.called) per body chunk

    async def send(message):
        if message["type"] == "http.response.body":
            span = spied_client["spans"][0]
            observed.append((message.get("more_body", False), span.finish.called))

    await middleware(scope, receive, send)

    # Exactly one http.server span; it finished "ok" after the body completed.
    assert len(spied_client["spans"]) == 1
    spied_client["spans"][0].finish.assert_called_once_with("ok")

    # While more chunks remained (more_body=True) the span was still open.
    assert observed, "streaming response produced no body chunks"
    for more_body, was_finished in observed:
        if more_body:
            assert was_finished is False, (
                "span finished before the streaming body completed"
            )


# ---------------------------------------------------------------------------
# User context
# ---------------------------------------------------------------------------
def test_user_context_from_request_state_dict(client, spied_client):
    resp = client.get("/me")
    assert resp.status_code == 200
    spied_client["set_user"].assert_called_once()
    kw = spied_client["set_user"].call_args.kwargs
    assert kw["user_id"] == "user-42"
    assert kw["email"] == "user42@example.com"


def test_user_context_from_request_state_object(client, spied_client):
    resp = client.get("/me-object")
    assert resp.status_code == 200
    spied_client["set_user"].assert_called_once()
    kw = spied_client["set_user"].call_args.kwargs
    assert kw["user_id"] == "obj-7"
    assert kw["email"] == "obj7@example.com"


def test_no_user_context_when_state_unset(client, spied_client):
    resp = client.get("/sync")
    assert resp.status_code == 200
    spied_client["set_user"].assert_not_called()


# ---------------------------------------------------------------------------
# Method gating: OPTIONS / HEAD excluded by default
# ---------------------------------------------------------------------------
def test_options_request_creates_no_span(client, spied_client):
    # OPTIONS is excluded by default; the request still works, just untracked.
    resp = client.options("/sync")
    # FastAPI's default OPTIONS handling returns 405 (no explicit handler) but
    # the middleware passes it through without creating an inbound span/record.
    assert resp.status_code in (200, 405)
    assert len(spied_client["spans"]) == 0
    assert len(spied_client["inbound_records"]()) == 0


def test_head_request_creates_no_span(client, spied_client):
    resp = client.head("/sync")
    assert resp.status_code in (200, 405)
    assert len(spied_client["spans"]) == 0
    assert len(spied_client["inbound_records"]()) == 0


def test_default_methods_exclude_options_and_head():
    assert "OPTIONS" not in DEFAULT_HTTP_METHODS_TO_CAPTURE
    assert "HEAD" not in DEFAULT_HTTP_METHODS_TO_CAPTURE
    assert "GET" in DEFAULT_HTTP_METHODS_TO_CAPTURE
    assert "POST" in DEFAULT_HTTP_METHODS_TO_CAPTURE


# ---------------------------------------------------------------------------
# Background tasks safety
# ---------------------------------------------------------------------------
def test_background_task_request_succeeds(client, spied_client):
    resp = client.get("/bg-task")
    assert resp.status_code == 200
    assert resp.json() == {"scheduled": True}
    # A successful background task is not an error.
    spied_client["capture_exception"].assert_not_called()
    spied_client["capture_error"].assert_not_called()
    spied_client["spans"][0].finish.assert_called_once_with("ok")


# ---------------------------------------------------------------------------
# Fail-open: an observability error must never break the host request
# ---------------------------------------------------------------------------
def test_capture_failure_does_not_break_request(app, spied_client):
    # Make the capture path itself blow up.
    spied_client["capture_exception"].side_effect = RuntimeError("telemetry down")
    spied_client["http_record"].side_effect = RuntimeError("record down")

    AllStakFastAPI(app, service="test-api")
    cl = TestClient(app, raise_server_exceptions=False)

    # The route raises → middleware tries to capture → capture itself raises.
    # The host must still get its 500, NOT the telemetry RuntimeError.
    resp = cl.get("/sync-raise")
    assert resp.status_code == 500


def test_telemetry_failure_does_not_break_healthy_request(app, spied_client):
    spied_client["http_record"].side_effect = RuntimeError("record down")
    spied_client["set_user"].side_effect = RuntimeError("user down")

    AllStakFastAPI(app, service="test-api")
    cl = TestClient(app, raise_server_exceptions=False)

    resp = cl.get("/me")
    # Despite recording + set_user failing, the 200 response is intact.
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_middleware_noop_when_sdk_not_initialized(app):
    # No init() → no client. The middleware must pass everything through.
    allstak_client._client = None
    allstak_client._initialized_once = False

    AllStakFastAPI(app, service="test-api")
    cl = TestClient(app, raise_server_exceptions=False)

    assert cl.get("/sync").status_code == 200
    assert cl.get("/items/5").json() == {"id": 5}
    assert cl.get("/not-found").status_code == 404
    assert cl.get("/sync-raise").status_code == 500


# ---------------------------------------------------------------------------
# Trace propagation headers
# ---------------------------------------------------------------------------
def test_response_carries_trace_headers(client, spied_client):
    resp = client.get("/sync")
    assert resp.status_code == 200
    # The send wrapper injects trace headers on the response.
    assert resp.headers.get("x-allstak-trace-id")
    assert resp.headers.get("traceparent", "").startswith("00-")


def test_incoming_trace_id_is_honored(client, spied_client):
    incoming = "abc123def4567890abc123def4567890"
    resp = client.get("/sync", headers={"x-allstak-trace-id": incoming})
    assert resp.status_code == 200
    assert resp.headers.get("x-allstak-trace-id") == incoming
