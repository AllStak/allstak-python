"""Standalone test suite for the Django ``AllStakMiddleware`` integration.

These tests configure Django entirely in-process (``settings.configure`` +
an inline urlconf) and drive requests through the middleware using both
``RequestFactory`` (direct middleware invocation) and the full Django test
``Client`` / ``AsyncClient`` (which exercises the real handler stack, exception
-> response conversion, and the ``got_request_exception`` signal path).

Nothing here touches the network: the SDK is initialised against a fake host
and behaviour is asserted by spying on the public ``allstak.*`` surface
(``capture_exception`` / ``set_user`` / ``http.record`` / ``start_span``).

What is asserted (parity-driven):
  * unhandled view exceptions are captured with mechanism handled=False, the
    request path/method in metadata, and re-raised so Django still renders 500;
  * a request span opens and finishes with the right status;
  * the span/transaction is named by the resolved route *pattern*, not the raw
    path with ids, when resolvable;
  * authenticated ``request.user`` identity (id/email) flows to ``set_user``;
  * 4xx (404/403/400) are NOT reported as errors;
  * inbound trace-id header propagation is honoured;
  * fail-open: a raising capture path never breaks the view response.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# --------------------------------------------------------------------------
# Configure Django once, in-process, before importing anything Django-y.
# --------------------------------------------------------------------------
import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        DEBUG=False,
        SECRET_KEY="allstak-test-secret",
        ALLOWED_HOSTS=["*", "testserver"],
        ROOT_URLCONF=__name__,  # urlpatterns are defined in this module
        MIDDLEWARE=["allstak.integrations.django.AllStakMiddleware"],
        DATABASES={},
        INSTALLED_APPS=[],
        TEMPLATES=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "APP_DIRS": False,
                "OPTIONS": {},
            }
        ],
        # The integration reads this block to self-initialise the SDK and to
        # pick up integration-only knobs.
        ALLSTAK={
            "api_key": "ask_test",
            "host": "http://allstak.test",
            "environment": "test",
        },
    )
    django.setup()

from django.core.exceptions import PermissionDenied, SuspiciousOperation  # noqa: E402
from django.http import (  # noqa: E402
    Http404,
    HttpResponse,
    HttpResponseServerError,
    JsonResponse,
)
from django.test import AsyncClient, Client, RequestFactory  # noqa: E402
from django.urls import path  # noqa: E402

import allstak  # noqa: E402
from allstak import client as allstak_client  # noqa: E402
from allstak.integrations import django as allstak_django  # noqa: E402
from allstak.integrations.django import AllStakMiddleware  # noqa: E402


# --------------------------------------------------------------------------
# Test urlconf — views that cover the matrix the parity checklist requires.
# --------------------------------------------------------------------------

def view_ok(request):
    return HttpResponse("ok")


def view_user_detail(request, pk):
    # Route pattern is /users/<int:pk>/ — used to assert route templating.
    return JsonResponse({"id": pk})


def view_raise(request):
    raise ValueError("boom from view")


def view_http404(request):
    raise Http404("missing")


def view_permission_denied(request):
    raise PermissionDenied("nope")


def view_suspicious(request):
    raise SuspiciousOperation("bad")


def view_explicit_500(request):
    # An explicitly *returned* 500 (nothing raised) must not be an error event.
    return HttpResponseServerError("server error body")


async def view_async_ok(request):
    return HttpResponse("async ok")


async def view_async_raise(request):
    raise RuntimeError("async boom")


urlpatterns = [
    path("ok/", view_ok, name="ok"),
    path("users/<int:pk>/", view_user_detail, name="user-detail"),
    path("raise/", view_raise, name="raise"),
    path("notfound/", view_http404, name="notfound"),
    path("forbidden/", view_permission_denied, name="forbidden"),
    path("suspicious/", view_suspicious, name="suspicious"),
    path("err500/", view_explicit_500, name="err500"),
    path("async-ok/", view_async_ok, name="async-ok"),
    path("async-raise/", view_async_raise, name="async-raise"),
]


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def sdk():
    """Reset the SDK singleton and the integration's one-shot signal latch.

    Yields the initialised client so tests can spy on it directly.
    """
    # Disconnect any previously-connected signal receiver and reset the latch
    # so each test re-connects against the *current* client instance.
    try:
        from django.core.signals import got_request_exception

        got_request_exception.disconnect(
            dispatch_uid=allstak_django._SIGNAL_DISPATCH_UID
        )
    except Exception:
        pass
    allstak_django._signal_connected = False

    allstak_client._client = None
    allstak_client._initialized_once = False
    allstak.init(api_key="ask_test", host="http://allstak.test", environment="test")

    yield allstak.get_client()

    try:
        from django.core.signals import got_request_exception

        got_request_exception.disconnect(
            dispatch_uid=allstak_django._SIGNAL_DISPATCH_UID
        )
    except Exception:
        pass
    allstak_django._signal_connected = False
    allstak_client._client = None
    allstak_client._initialized_once = False


@pytest.fixture
def spy(sdk, monkeypatch):
    """Install MagicMock spies over the public reporting surface.

    Returns a namespace with ``capture_exception`` / ``set_user`` /
    ``http_record`` / ``start_span`` recorders. ``start_span`` returns real
    spans (wrapping the live tracer) so span lifecycle can be asserted.
    """
    client = sdk

    capture = MagicMock(return_value="evt-test")
    set_user = MagicMock()
    http_record = MagicMock()

    started_spans = []
    real_start_span = client.start_span

    def start_span(operation, *, description="", tags=None):
        span = real_start_span(operation, description=description, tags=tags)
        started_spans.append(span)
        return span

    monkeypatch.setattr(client, "capture_exception", capture)
    monkeypatch.setattr(client, "set_user", set_user)
    monkeypatch.setattr(client.http, "record", http_record)
    monkeypatch.setattr(client, "start_span", MagicMock(side_effect=start_span))

    ns = MagicMock()
    ns.client = client
    ns.capture_exception = capture
    ns.set_user = set_user
    ns.http_record = http_record
    ns.start_span = client.start_span
    ns.spans = started_spans
    return ns


def _mw(view):
    """Build the middleware around a single bare view (sync RequestFactory path)."""
    return AllStakMiddleware(view)


# ==========================================================================
# Span lifecycle + route templating
# ==========================================================================

def test_request_span_opens_and_finishes_ok(spy):
    request = RequestFactory().get("/ok/")
    response = _mw(view_ok)(request)

    assert response.status_code == 200
    assert spy.start_span.called
    span = spy.spans[0]
    assert span.is_finished is True
    assert span._status == "ok"
    assert span._tags["http.method"] == "GET"
    assert span._tags["http.status_code"] == "200"


def test_span_named_by_route_pattern_not_raw_path(spy):
    # /users/42/ should be templated to the route pattern, not the concrete id.
    # Driven through the full handler so URL routing actually supplies ``pk``.
    client = Client()
    response = client.get("/users/42/")

    assert response.status_code == 200
    span = spy.spans[0]
    route = span._tags["http.route"]
    assert "<int:pk>" in route
    assert "42" not in route
    assert route == "/users/<int:pk>/"


def test_route_resolution_direct_invocation(spy):
    # Direct middleware invocation still resolves the route pattern for the
    # span name even though it cannot dispatch a parameterised view. We wrap
    # the bare view so the call itself succeeds while the path stays /users/7/.
    request = RequestFactory().get("/users/7/")
    response = _mw(lambda req: HttpResponse("ok"))(request)

    assert response.status_code == 200
    assert spy.spans[0]._tags["http.route"] == "/users/<int:pk>/"


def test_unresolvable_path_falls_back_to_raw_path(spy):
    # No urlpattern matches /totally/unknown/ -> raw path fallback (source=url).
    request = RequestFactory().get("/totally/unknown/")
    response = _mw(view_ok)(request)

    assert response.status_code == 200
    span = spy.spans[0]
    assert span._tags["http.route"] == "/totally/unknown/"


def test_http_record_emitted_with_request_metadata(spy):
    request = RequestFactory().get("/ok/")
    _mw(view_ok)(request)

    assert spy.http_record.called
    kwargs = spy.http_record.call_args.kwargs
    assert kwargs["direction"] == "inbound"
    assert kwargs["method"] == "GET"
    assert kwargs["path"] == "/ok/"
    assert kwargs["status_code"] == 200
    assert kwargs["error_fingerprint"] is None


# ==========================================================================
# Unhandled exceptions — captured via got_request_exception, re-raised so
# Django still renders 500.
# ==========================================================================

def test_unhandled_view_exception_captured_via_signal_and_500_rendered(spy):
    # Full handler stack: the ValueError is converted to a 500 response *and*
    # got_request_exception fires -> our receiver captures it.
    client = Client(raise_request_exception=False)
    response = client.get("/raise/")

    # Django still renders its own 500 (exception re-raised, not swallowed).
    assert response.status_code == 500

    assert spy.capture_exception.called
    args, kwargs = spy.capture_exception.call_args
    captured_exc = args[0]
    assert isinstance(captured_exc, ValueError)
    assert kwargs["mechanism"] == {"type": "django", "handled": False}
    meta = kwargs["metadata"]
    assert meta["http.path"] == "/raise/"
    assert meta["http.method"] == "GET"
    # Request context carries path + method as well.
    req_ctx = kwargs["request_context"]
    assert req_ctx.path == "/raise/"
    assert req_ctx.method == "GET"


def test_unhandled_exception_reraised_on_direct_invocation(spy):
    # Direct RequestFactory invocation: the view's exception propagates out of
    # the middleware (re-raised) and is captured at the boundary fallback.
    request = RequestFactory().get("/raise/")
    with pytest.raises(ValueError):
        _mw(view_raise)(request)

    assert spy.capture_exception.called
    args, kwargs = spy.capture_exception.call_args
    assert isinstance(args[0], ValueError)
    assert kwargs["mechanism"] == {"type": "django", "handled": False}
    assert kwargs["request_context"].path == "/raise/"


def test_exception_not_double_reported(spy):
    # Going through the full stack: signal fires once; the boundary fallback
    # must not also report (the handler turns the error into a 500 response,
    # so the boundary never even sees a raised exception here). Either way,
    # exactly one capture.
    client = Client(raise_request_exception=False)
    client.get("/raise/")
    assert spy.capture_exception.call_count == 1


def test_span_status_error_on_unhandled_exception(spy):
    client = Client(raise_request_exception=False)
    client.get("/raise/")

    assert spy.spans, "a request span should have been opened"
    span = spy.spans[0]
    assert span.is_finished is True
    assert span._status == "error"
    assert span._tags["http.status_code"] == "500"


# ==========================================================================
# 4xx are NOT reported as errors.
# ==========================================================================

@pytest.mark.parametrize(
    "url,expected_status",
    [
        ("/notfound/", 404),
        ("/forbidden/", 403),
        ("/suspicious/", 400),
    ],
)
def test_4xx_not_reported_as_errors_full_stack(spy, url, expected_status):
    # Through the real handler, Django maps these to responses *before* the
    # got_request_exception signal -> they are never captured as errors.
    client = Client(raise_request_exception=False)
    response = client.get(url)

    assert response.status_code == expected_status
    assert spy.capture_exception.called is False
    # The transaction still reflects the 4xx status, but as a non-error span.
    assert spy.spans
    span = spy.spans[0]
    assert span._tags["http.status_code"] == str(expected_status)
    assert span._status == "ok"


@pytest.mark.parametrize(
    "view,exc",
    [
        (view_http404, Http404),
        (view_permission_denied, PermissionDenied),
        (view_suspicious, SuspiciousOperation),
    ],
)
def test_4xx_exceptions_not_captured_on_direct_invocation(spy, view, exc):
    # Direct invocation re-raises the framework exception (no handler to map
    # it), but the integration must still refuse to report it as an error.
    request = RequestFactory().get("/notfound/")
    with pytest.raises(exc):
        _mw(view)(request)
    assert spy.capture_exception.called is False


def test_explicit_500_response_not_captured_as_error(spy):
    # Nothing is raised; a 500 is *returned*. No error event, only status.
    request = RequestFactory().get("/err500/")
    response = _mw(view_explicit_500)(request)

    assert response.status_code == 500
    assert spy.capture_exception.called is False
    span = spy.spans[0]
    assert span._tags["http.status_code"] == "500"
    # Status reflects the 5xx even though nothing was captured.
    assert span._status == "error"


# ==========================================================================
# User context flows to set_user when authenticated.
# ==========================================================================

class _FakeUser:
    def __init__(self, pk, email, authenticated=True):
        self.pk = pk
        self.email = email
        self._auth = authenticated

    @property
    def is_authenticated(self):
        return self._auth


def test_authenticated_user_identity_flows_to_set_user(spy):
    request = RequestFactory().get("/ok/")
    request.user = _FakeUser(pk=4321, email="dev@allstak.test")
    _mw(view_ok)(request)

    assert spy.set_user.called
    kwargs = spy.set_user.call_args.kwargs
    assert kwargs["user_id"] == "4321"
    assert kwargs["email"] == "dev@allstak.test"


def test_anonymous_user_not_sent(spy):
    request = RequestFactory().get("/ok/")
    request.user = _FakeUser(pk=None, email=None, authenticated=False)
    _mw(view_ok)(request)

    assert spy.set_user.called is False


def test_no_user_attribute_does_not_break(spy):
    request = RequestFactory().get("/ok/")
    # No request.user at all (e.g. AuthenticationMiddleware not installed).
    response = _mw(view_ok)(request)
    assert response.status_code == 200
    assert spy.set_user.called is False


# ==========================================================================
# Trace-id propagation from inbound headers.
# ==========================================================================

def test_inbound_traceparent_header_honoured(spy):
    incoming_trace = "a" * 32
    request = RequestFactory().get(
        "/ok/", HTTP_TRACEPARENT=f"00-{incoming_trace}-bbbbbbbbbbbbbbbb-01"
    )
    response = _mw(view_ok)(request)

    # The span (and propagated response headers) adopt the inbound trace id.
    assert spy.spans[0].trace_id == incoming_trace
    assert response["x-allstak-trace-id"] == incoming_trace
    # http.record is correlated to the same inbound trace.
    assert spy.http_record.call_args.kwargs["trace_id"] == incoming_trace


def test_inbound_x_allstak_trace_id_header_honoured(spy):
    incoming_trace = "deadbeefdeadbeefdeadbeefdeadbeef"
    request = RequestFactory().get("/ok/", HTTP_X_ALLSTAK_TRACE_ID=incoming_trace)
    _mw(view_ok)(request)

    assert spy.spans[0].trace_id == incoming_trace


def test_response_carries_trace_headers_when_no_inbound(spy):
    request = RequestFactory().get("/ok/")
    response = _mw(view_ok)(request)

    # A fresh trace id is generated and stamped onto the response.
    assert response.get("x-allstak-trace-id")
    assert response.get("traceparent", "").startswith("00-")


# ==========================================================================
# Async support.
# ==========================================================================

@pytest.mark.asyncio
async def test_async_view_returns_200(spy):
    client = AsyncClient()
    response = await client.get("/async-ok/")
    assert response.status_code == 200
    assert response.content == b"async ok"
    # The async path still records inbound telemetry + a span.
    assert spy.http_record.called
    assert spy.spans
    assert spy.spans[0].is_finished is True


@pytest.mark.asyncio
async def test_async_unhandled_exception_captured(spy):
    client = AsyncClient(raise_request_exception=False)
    response = await client.get("/async-raise/")
    assert response.status_code == 500

    assert spy.capture_exception.called
    args, kwargs = spy.capture_exception.call_args
    assert isinstance(args[0], RuntimeError)
    assert kwargs["mechanism"] == {"type": "django", "handled": False}
    assert kwargs["metadata"]["http.path"] == "/async-raise/"


def test_middleware_detects_async_mode():
    async def async_get_response(request):
        return HttpResponse("ok")

    def sync_get_response(request):
        return HttpResponse("ok")

    assert _mw(sync_get_response)._is_async is False
    assert AllStakMiddleware(async_get_response)._is_async is True


# ==========================================================================
# Fail-open: a raising capture path must never break the host response.
# ==========================================================================

def test_fail_open_capture_exception_raising_does_not_break_view(sdk, monkeypatch):
    # Force capture_exception to blow up — the request must still complete and
    # the original view exception must still propagate (so Django renders 500).
    boom = MagicMock(side_effect=RuntimeError("capture exploded"))
    monkeypatch.setattr(sdk, "capture_exception", boom)

    request = RequestFactory().get("/raise/")
    # The view's ValueError is re-raised (Django would render 500); the
    # observability failure inside the boundary capture is swallowed.
    with pytest.raises(ValueError):
        _mw(view_raise)(request)


def test_fail_open_capture_raising_full_stack_still_renders_500(sdk, monkeypatch):
    boom = MagicMock(side_effect=RuntimeError("capture exploded"))
    monkeypatch.setattr(sdk, "capture_exception", boom)

    client = Client(raise_request_exception=False)
    response = client.get("/raise/")
    # Even though the signal receiver's capture raised internally, Django still
    # renders its 500 — the host request is unaffected.
    assert response.status_code == 500


def test_fail_open_start_span_raising_does_not_break_view(sdk, monkeypatch):
    monkeypatch.setattr(
        sdk, "start_span", MagicMock(side_effect=RuntimeError("span exploded"))
    )
    request = RequestFactory().get("/ok/")
    response = _mw(view_ok)(request)
    assert response.status_code == 200


def test_fail_open_http_record_raising_does_not_break_view(sdk, monkeypatch):
    monkeypatch.setattr(
        sdk.http, "record", MagicMock(side_effect=RuntimeError("record exploded"))
    )
    request = RequestFactory().get("/ok/")
    response = _mw(view_ok)(request)
    assert response.status_code == 200


def test_fail_open_set_user_raising_does_not_break_view(sdk, monkeypatch):
    monkeypatch.setattr(
        sdk, "set_user", MagicMock(side_effect=RuntimeError("set_user exploded"))
    )
    request = RequestFactory().get("/ok/")
    request.user = _FakeUser(pk=1, email="x@y.z")
    response = _mw(view_ok)(request)
    assert response.status_code == 200


def test_middleware_no_op_when_sdk_not_initialised(monkeypatch):
    # No client at all: middleware must pass the request through untouched.
    try:
        from django.core.signals import got_request_exception

        got_request_exception.disconnect(
            dispatch_uid=allstak_django._SIGNAL_DISPATCH_UID
        )
    except Exception:
        pass
    allstak_django._signal_connected = False
    allstak_client._client = None
    allstak_client._initialized_once = False

    # Stop _ensure_initialized from re-creating a client from settings.
    monkeypatch.setattr(allstak_django.AllStakMiddleware, "_ensure_initialized", lambda self: None)

    request = RequestFactory().get("/ok/")
    response = _mw(view_ok)(request)
    assert response.status_code == 200
