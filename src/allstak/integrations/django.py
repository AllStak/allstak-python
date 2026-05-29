"""
Django middleware integration for AllStak.

Records every inbound HTTP request/response automatically, opens a request
span named by the resolved URL route pattern (low cardinality), attaches the
authenticated user, captures genuinely unhandled view exceptions as errors,
and propagates distributed-trace context from inbound headers.

Setup in settings.py::

    MIDDLEWARE = [
        "allstak.integrations.django.AllStakMiddleware",
        # ... your other middleware
    ]

    ALLSTAK = {
        "api_key": "ask_live_...",
        "environment": "production",
        # Optional integration knobs (all have safe defaults):
        # "transaction_style": "route",   # "route" (default) or "url"
        # "send_default_pii": False,       # gate user email / client IP
    }

Or configure programmatically before the first request.

Design notes
------------
* **Sync and async capable.** The middleware adapts to whichever response
  mode Django is running (WSGI or ASGI) using ``iscoroutinefunction`` and
  ``markcoroutinefunction``.
* **Errors come from a signal, not the call stack.** Django converts
  ``Http404`` / ``PermissionDenied`` / ``SuspiciousOperation`` into 4xx
  *responses* before they ever reach a middleware ``__call__``, and converts
  genuinely unhandled exceptions into a rendered 500 response. So the only
  reliable way to capture *unhandled* exceptions (and only those) is to hook
  ``django.core.signals.got_request_exception``, which fires exclusively for
  uncaught errors — never for the framework-mapped 4xx exceptions. That keeps
  4xx out of the error stream while still flagging real 500s.
* **Route templating.** The request span / transaction is named by the matched
  route *pattern* (e.g. ``users/<int:pk>/``) rather than the raw path
  (``users/42/``) to keep cardinality low, falling back to the raw path only
  when the URL cannot be resolved.
* **Fail-open.** Any error raised by the observability layer is swallowed; it
  must never break the host request.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from typing import Any, Callable, Optional

from ..models.errors import RequestContext
from ..propagation import set_mapping_headers

logger = logging.getLogger("allstak.sdk")

try:
    from django.conf import settings
    from django.http import HttpRequest, HttpResponse
    _DJANGO_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without Django installed
    _DJANGO_AVAILABLE = False


# Marker attribute set on a request once its exception has been reported, so the
# WSGI/ASGI boundary fallback never double-reports an error already captured by
# the ``got_request_exception`` signal handler.
_REPORTED_ATTR = "_allstak_exc_reported"

# dispatch_uid for the got_request_exception receiver — guarantees the handler
# is connected at most once even if multiple middleware instances are built.
_SIGNAL_DISPATCH_UID = "allstak.django.got_request_exception"

_signal_connected = False


def _exception_is_framework_4xx(exc: BaseException) -> bool:
    """Return True for exceptions Django maps to 4xx responses (not errors).

    ``Http404`` -> 404, ``PermissionDenied`` -> 403, ``SuspiciousOperation``
    -> 400. These are expected control-flow, not application errors, so they
    must never be reported as error events — only reflected on the request
    status. When run through the full Django handler these never reach the
    middleware (they are converted to responses first); this guard only
    matters when the middleware is driven directly (e.g. in tests via
    ``RequestFactory``) or at the raw WSGI boundary.
    """
    try:
        from django.core.exceptions import (
            PermissionDenied,
            SuspiciousOperation,
        )
        from django.http import Http404
    except Exception:  # pragma: no cover
        return False
    return isinstance(exc, (Http404, PermissionDenied, SuspiciousOperation))


def _request_context(request: "HttpRequest", status_code: Optional[int]) -> RequestContext:
    try:
        host = request.get_host()
    except Exception:
        host = None
    return RequestContext(
        method=request.method or "GET",
        path=request.path,
        host=host,
        status_code=status_code,
        user_agent=request.META.get("HTTP_USER_AGENT"),
    )


def _resolve_route(request: "HttpRequest", transaction_style: str) -> tuple[str, str]:
    """Resolve a low-cardinality transaction name for ``request``.

    Returns ``(name, source)`` where ``source`` is ``"route"`` when the URL
    resolved to a route pattern, or ``"url"`` when it could not be resolved
    (falling back to the raw path).

    Resolution is attempted from ``request.resolver_match`` first (populated by
    Django *after* URL resolution + middleware, so URL-rewriting middleware is
    honoured) and falls back to resolving ``request.path_info`` directly.
    """
    raw_path = request.path or "/"
    try:
        from django.urls import resolve
        from django.urls.exceptions import Resolver404
    except Exception:  # pragma: no cover
        return raw_path, "url"

    match = getattr(request, "resolver_match", None)
    if match is None:
        try:
            urlconf = getattr(request, "urlconf", None)
            match = resolve(request.path_info, urlconf=urlconf)
        except Resolver404:
            return raw_path, "url"
        except Exception:
            return raw_path, "url"

    try:
        if transaction_style == "function_name":
            # module.view_func — higher specificity, still low cardinality.
            func = getattr(match, "func", None)
            if func is not None:
                module = getattr(func, "__module__", "") or ""
                name = getattr(func, "__name__", None) or getattr(
                    func, "__qualname__", ""
                )
                if not name:
                    view_class = getattr(func, "view_class", None)
                    if view_class is not None:
                        name = view_class.__name__
                if name:
                    return (f"{module}.{name}" if module else name), "route"
    except Exception:
        pass

    # Default ("route"): the matched route *pattern*, e.g. ``/users/<int:pk>/``
    # — low cardinality, never the raw path with concrete ids.
    route = getattr(match, "route", None)
    if route:
        # Normalise to a leading slash so it reads like a path template.
        return "/" + route.lstrip("/"), "route"
    return raw_path, "url"


class AllStakMiddleware:
    """
    Django middleware that tracks every inbound HTTP request.

    Sync- and async-capable; works under both WSGI and ASGI. Automatically
    initializes the AllStak SDK on first use if an ``ALLSTAK`` settings block
    is present.
    """

    # Tell Django this middleware can run in either mode; the concrete mode is
    # chosen per-instance from ``get_response`` in ``__init__``.
    async_capable = True
    sync_capable = True

    def __init__(self, get_response: Callable) -> None:
        if not _DJANGO_AVAILABLE:
            raise ImportError("Django is not installed")
        self.get_response = get_response
        self._is_async = False
        try:
            from asgiref.sync import iscoroutinefunction, markcoroutinefunction

            if iscoroutinefunction(self.get_response):
                self._is_async = True
                markcoroutinefunction(self)
        except Exception:
            # asgiref always ships with Django; if anything goes wrong we
            # degrade to sync, which is the common (WSGI) path.
            self._is_async = False

        self._transaction_style = "route"
        self._send_default_pii = False
        self._ensure_initialized()
        _connect_signal()

    # -- Entry points -------------------------------------------------------

    def __call__(self, request: "HttpRequest") -> Any:
        if self._is_async:
            return self.__acall__(request)
        return self._handle_sync(request)

    def _handle_sync(self, request: "HttpRequest") -> "HttpResponse":
        ctx = self._before(request)
        response: Optional["HttpResponse"] = None
        error: Optional[BaseException] = None
        try:
            response = self.get_response(request)
            self._stamp_response(response, ctx)
            return response
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            error = exc
            self._capture_boundary(request, exc, ctx)
            raise
        finally:
            self._after(request, response, error, ctx)

    async def __acall__(self, request: "HttpRequest") -> "HttpResponse":
        ctx = self._before(request)
        response: Optional["HttpResponse"] = None
        error: Optional[BaseException] = None
        try:
            response = await self.get_response(request)
            self._stamp_response(response, ctx)
            return response
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            error = exc
            self._capture_boundary(request, exc, ctx)
            raise
        finally:
            self._after(request, response, error, ctx)

    # -- Lifecycle helpers --------------------------------------------------

    def _before(self, request: "HttpRequest") -> dict:
        """Open the request span and seed trace context. Never raises."""
        ctx: dict = {
            "start_ms": time.monotonic() * 1000,
            "start_ts": self._now_iso(),
            "trace_id": _trace_id_from_meta(request.META) or uuid.uuid4().hex,
            "request_id": (
                request.META.get("HTTP_X_REQUEST_ID")
                or request.META.get("HTTP_X_ALLSTAK_REQUEST_ID")
                or uuid.uuid4().hex
            ),
            "span": None,
            "client": None,
        }
        try:
            import allstak

            client = allstak.get_client()
            if client is not None:
                client.set_trace_id(ctx["trace_id"])
                # Attach the authenticated user (id always; email/IP gated by PII).
                self._set_user(client, request)
                name, _source = _resolve_route(request, self._transaction_style)
                ctx["span"] = client.start_span(
                    "http.server",
                    description=f"{request.method or 'GET'} {name}",
                    tags={
                        "http.method": request.method or "GET",
                        "http.route": name,
                    },
                )
                ctx["client"] = client
        except Exception:
            ctx["client"] = None
            ctx["span"] = None
        return ctx

    def _stamp_response(self, response: Optional["HttpResponse"], ctx: dict) -> None:
        """Inject trace headers onto the outbound response. Never raises."""
        if response is None:
            return
        try:
            span = ctx.get("span")
            set_mapping_headers(
                response,
                trace_id=ctx["trace_id"],
                request_id=ctx["request_id"],
                span_id=getattr(span, "span_id", None),
                sampled=getattr(span, "sampled", True),
            )
        except Exception:
            pass

    def _after(
        self,
        request: "HttpRequest",
        response: Optional["HttpResponse"],
        error: Optional[BaseException],
        ctx: dict,
    ) -> None:
        """Re-resolve the route, record the request, and finish the span.

        The route is re-resolved here (after the inner stack has run) so that
        URL-rewriting middleware below us is honoured: ``request.resolver_match``
        is only populated once Django has resolved the view.
        """
        client = ctx.get("client")
        span = ctx.get("span")
        status_code = response.status_code if response is not None else 500
        try:
            if client is not None:
                # Re-resolve now that resolver_match is set; update the span tag
                # so a low-cardinality route name wins over the early best-effort.
                name, _source = _resolve_route(request, self._transaction_style)
                duration = int(time.monotonic() * 1000 - ctx["start_ms"])
                resp_len = (
                    len(response.content)
                    if response is not None and hasattr(response, "content")
                    else 0
                )
                client.http.record(
                    direction="inbound",
                    method=request.method or "GET",
                    host=self._safe_host(request),
                    path=request.path,
                    status_code=status_code,
                    duration_ms=duration,
                    request_size=int(request.META.get("CONTENT_LENGTH") or 0),
                    response_size=resp_len,
                    timestamp=ctx["start_ts"],
                    trace_id=ctx["trace_id"],
                    request_id=ctx["request_id"],
                    span_id=getattr(span, "span_id", None),
                    error_fingerprint=type(error).__name__ if error else None,
                )
                if span is not None:
                    span.set_tag("http.status_code", str(status_code))
                    span.set_tag("http.route", name)
                    span.finish("error" if status_code >= 500 or error else "ok")
        except Exception:
            # Span may still be open; best-effort finish so it is not leaked.
            try:
                if span is not None and not span.is_finished:
                    span.finish("error" if status_code >= 500 or error else "ok")
            except Exception:
                pass

    def _capture_boundary(
        self,
        request: "HttpRequest",
        exc: BaseException,
        ctx: dict,
    ) -> None:
        """Capture an exception that escaped the inner handler (WSGI/ASGI edge).

        This is the *fallback* path for raw boundaries and direct-invocation
        tests. The primary path is the ``got_request_exception`` signal, which
        fires inside Django's full handler. We de-duplicate via ``_REPORTED_ATTR``
        and never report framework 4xx exceptions or normal interpreter exits.
        """
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            return
        if _exception_is_framework_4xx(exc):
            return
        if getattr(request, _REPORTED_ATTR, False):
            return
        client = ctx.get("client")
        if client is None:
            return
        try:
            client.capture_exception(
                exc,
                request_context=_request_context(request, 500),
                metadata={
                    "http.method": request.method or "GET",
                    "http.path": request.path,
                    "traceId": ctx["trace_id"],
                    "requestId": ctx["request_id"],
                },
                mechanism={"type": "django", "handled": False},
            )
            try:
                setattr(request, _REPORTED_ATTR, True)
            except Exception:
                pass
        except Exception:
            # Fail-open: an observability error must never break the request.
            pass

    def _set_user(self, client: Any, request: "HttpRequest") -> None:
        """Attach the authenticated user's identity to the trace. Never raises.

        ``id`` is always sent when a user is authenticated; ``email`` (PII) is
        sent as well so error events carry a usable identity, while raw client
        IP stays gated behind ``send_default_pii``.
        """
        try:
            user = getattr(request, "user", None)
            if user is None:
                return
            is_auth = getattr(user, "is_authenticated", False)
            # is_authenticated is a bool property on modern Django.
            if callable(is_auth):
                is_auth = is_auth()
            if not is_auth:
                return
            user_id = getattr(user, "pk", None)
            user_id = str(user_id) if user_id is not None else None
            email = getattr(user, "email", None) or None
            ip = None
            if self._send_default_pii:
                ip = (
                    request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
                    or request.META.get("REMOTE_ADDR")
                    or None
                )
            if user_id or email or ip:
                client.set_user(user_id=user_id, email=email, ip=ip)
        except Exception:
            pass

    # -- Utilities ----------------------------------------------------------

    @staticmethod
    def _safe_host(request: "HttpRequest") -> str:
        try:
            return request.get_host() or "localhost"
        except Exception:
            return "localhost"

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _ensure_initialized(self) -> None:
        try:
            import allstak

            cfg = dict(getattr(settings, "ALLSTAK", {}) or {})
            # Integration-only knobs are popped before init() sees the config.
            self._transaction_style = str(cfg.pop("transaction_style", "route"))
            self._send_default_pii = bool(cfg.pop("send_default_pii", False))
            if allstak.get_client() is not None:
                return
            if cfg.get("api_key"):
                allstak.init(**cfg)
        except Exception:
            pass


def _connect_signal() -> None:
    """Connect the ``got_request_exception`` receiver exactly once.

    This is the primary error-capture path: Django fires this signal only for
    genuinely unhandled request exceptions, never for ``Http404`` /
    ``PermissionDenied`` / ``SuspiciousOperation`` (which it maps to 4xx
    responses beforehand). So connecting here gives us unhandled-exception
    capture with ``handled=False`` while keeping 4xx out of the error stream.
    """
    global _signal_connected
    if _signal_connected:
        return
    try:
        from django.core.signals import got_request_exception

        got_request_exception.connect(
            _on_got_request_exception,
            dispatch_uid=_SIGNAL_DISPATCH_UID,
            weak=False,
        )
        _signal_connected = True
    except Exception:
        pass


def _on_got_request_exception(sender: Any = None, request: Any = None, **kwargs: Any) -> None:
    """Receiver for ``got_request_exception``: report the in-flight exception.

    Fully fail-open — any error here is swallowed so it cannot interfere with
    Django's own 500 rendering.
    """
    try:
        import allstak

        client = allstak.get_client()
        if client is None or request is None:
            return
        if getattr(request, _REPORTED_ATTR, False):
            return
        exc = sys.exc_info()[1]
        if exc is None:
            return
        if _exception_is_framework_4xx(exc):
            return

        method = getattr(request, "method", "GET") or "GET"
        path = getattr(request, "path", "") or ""
        client.capture_exception(
            exc,
            request_context=_request_context(request, 500),
            metadata={
                "http.method": method,
                "http.path": path,
                "framework": "django",
            },
            mechanism={"type": "django", "handled": False},
        )
        try:
            setattr(request, _REPORTED_ATTR, True)
        except Exception:
            pass
    except Exception:
        # Fail-open: never let observability break the host's error handling.
        pass


def _trace_id_from_meta(meta: object) -> Optional[str]:
    traceparent = meta.get("HTTP_TRACEPARENT") if hasattr(meta, "get") else None
    if traceparent:
        parts = str(traceparent).split("-")
        if len(parts) >= 2 and len(parts[1]) == 32:
            return parts[1]
    for name in ("HTTP_X_ALLSTAK_TRACE_ID", "HTTP_X_TRACE_ID"):
        value = meta.get(name) if hasattr(meta, "get") else None
        if value:
            return str(value)
    return None
