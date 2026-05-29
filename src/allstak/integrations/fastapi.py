"""
FastAPI / Starlette integration for AllStak.

Automatically captures:

- Inbound HTTP request telemetry (method, path, status, duration, size).
- Unhandled exceptions from routes and dependencies (surfaced as errors).
- 5xx responses — including framework ``HTTPException(status_code=5xx)`` that
  never bubble out as a raised exception — are reported. 4xx client errors are
  **not** reported by default (configurable via ``failed_request_status_codes``).
- Per-request trace ID (from ``traceparent`` / ``x-allstak-trace-id`` /
  ``x-request-id`` or newly generated).
- User context from ``request.state.user`` if your auth dependency sets it.

The request span / transaction is named by the matched **route template**
(e.g. ``/items/{item_id}``) rather than the concrete path (``/items/42``) so
high-cardinality URLs collapse to a single low-cardinality name.

Setup::

    from fastapi import FastAPI
    import allstak
    from allstak.integrations.fastapi import AllStakFastAPI

    allstak.init(api_key="ask_live_...", host="https://ingest.allstak.dev")

    app = FastAPI()
    AllStakFastAPI(app, service="my-api")

The integration is a pure ASGI middleware — it works with any Starlette-based
framework (FastAPI, Starlette, Litestar via Starlette compat, etc.).

Design notes
------------

* **Capture is status-code driven, not exception-type driven.** Any response
  whose status code falls in ``failed_request_status_codes`` (default: the
  whole ``range(500, 600)``) is reported. An unhandled exception that bubbles
  out of the inner app is treated as a 500 and reported. A
  ``HTTPException(status_code=404)`` (or any 4xx) is converted to a response by
  the framework's exception handler and is deliberately **not** reported, to
  avoid noise from client errors.
* **Captured server errors are marked ``handled=True``** with an integration
  ``type`` tag, because the framework's exception middleware catches them
  before they reach the ASGI server.
* **Fail-open.** Any error inside this middleware (recording telemetry,
  capturing an exception, reading user context, …) is swallowed — observability
  must never break the host request.
* **Low cardinality.** Span / transaction names use the route template from
  ``scope["route"].path``. Unmatched routes (404 with no route) fall back to a
  generic name.
* **OPTIONS / HEAD are excluded** from transaction creation by default
  (``http_methods_to_capture``) to cut CORS-preflight / health-check noise.
* **Streaming & background tasks.** The span finishes only after the final
  response body chunk (``more_body`` is false), so streaming generators do not
  truncate the span. Errors raised after the response starts (streaming
  generators / background tasks) still reach the ASGI error path and are
  reported when they map to a 5xx.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Set

from ..propagation import set_asgi_headers

logger = logging.getLogger("allstak.sdk")

try:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    _STARLETTE_AVAILABLE = True
except ImportError:
    _STARLETTE_AVAILABLE = False


#: Default set of response status codes that are reported as errors.
#: Mirrors the framework convention of treating all 5xx as server errors.
DEFAULT_FAILED_REQUEST_STATUS_CODES: Set[int] = set(range(500, 600))

#: HTTP methods that create a request transaction / span by default.
#: OPTIONS and HEAD are excluded to avoid CORS-preflight / health-check noise.
DEFAULT_HTTP_METHODS_TO_CAPTURE: Set[str] = {
    "CONNECT",
    "DELETE",
    "GET",
    "PATCH",
    "POST",
    "PUT",
    "TRACE",
}

#: Fallback transaction name when no route matched (e.g. a 404 to an unmapped
#: path) so the name is never null / a raw high-cardinality URL.
_DEFAULT_TRANSACTION_NAME = "generic ASGI request"

#: ``mechanism.type`` tag attached to captured server errors so the UI can tell
#: the framework caught them rather than the process crashing.
_MECHANISM_TYPE = "fastapi"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_status_codes(value: Optional[Iterable[int]]) -> Set[int]:
    if value is None:
        return set(DEFAULT_FAILED_REQUEST_STATUS_CODES)
    try:
        return {int(code) for code in value}
    except Exception:
        return set(DEFAULT_FAILED_REQUEST_STATUS_CODES)


def _normalize_methods(value: Optional[Iterable[str]]) -> Set[str]:
    if value is None:
        return set(DEFAULT_HTTP_METHODS_TO_CAPTURE)
    try:
        return {str(m).upper() for m in value}
    except Exception:
        return set(DEFAULT_HTTP_METHODS_TO_CAPTURE)


def _transaction_name(scope: "Scope", transaction_style: str) -> str:
    """Resolve a low-cardinality transaction / span name from the ASGI scope.

    ``transaction_style`` controls the source:

    * ``"url"`` (default): the matched route **template**
      (``scope["route"].path``), e.g. ``/items/{item_id}``.
    * ``"endpoint"``: the handler function name (``scope["endpoint"]``).

    Falls back to :data:`_DEFAULT_TRANSACTION_NAME` when nothing matched (404 /
    unmatched path) so the name is never the raw concrete URL.
    """
    try:
        if transaction_style == "endpoint":
            endpoint = scope.get("endpoint")
            if endpoint is not None:
                name = getattr(endpoint, "__name__", None)
                if name:
                    return str(name)
        # Default "url" style — route template, never the concrete path.
        route = scope.get("route")
        route_path = getattr(route, "path", None)
        if route_path:
            return str(route_path)
    except Exception:
        pass
    return _DEFAULT_TRANSACTION_NAME


def _extract_user(scope: "Scope") -> Optional[dict]:
    """Pull ``request.state.user`` (``scope["state"]["user"]``) if present.

    Supports either a mapping (``{"id": ..., "email": ..., "ip": ...}``) or an
    object exposing ``id`` / ``user_id`` / ``email`` / ``ip`` attributes.
    Returns a dict of the keyword args accepted by :func:`allstak.set_user`, or
    ``None`` when no usable identity is available.
    """
    try:
        state = scope.get("state") or {}
        user = state.get("user") if hasattr(state, "get") else None
        if user is None:
            return None

        def _get(key: str) -> Any:
            if isinstance(user, dict):
                return user.get(key)
            return getattr(user, key, None)

        user_id = _get("id") or _get("user_id") or _get("username")
        email = _get("email")
        ip = _get("ip") or _get("ip_address")
        if user_id is None and email is None and ip is None:
            return None
        out: dict = {}
        if user_id is not None:
            out["user_id"] = str(user_id)
        if email is not None:
            out["email"] = str(email)
        if ip is not None:
            out["ip"] = str(ip)
        return out
    except Exception:
        return None


class AllStakASGIMiddleware:
    """
    Pure ASGI middleware that records inbound HTTP telemetry, trace context,
    user context, and server errors for every request.

    :param app: The wrapped ASGI application.
    :param service: Optional service name attached to the trace.
    :param transaction_style: ``"url"`` (route template, default) or
        ``"endpoint"`` (handler function name).
    :param failed_request_status_codes: Response status codes reported as
        errors. Defaults to every ``5xx`` code; pass e.g.
        ``{400, *range(500, 600)}`` to also report ``400`` responses.
    :param http_methods_to_capture: HTTP methods that create a span /
        transaction. Defaults exclude ``OPTIONS`` and ``HEAD``.
    """

    def __init__(
        self,
        app: "ASGIApp",
        service: str = "",
        *,
        transaction_style: str = "url",
        failed_request_status_codes: Optional[Iterable[int]] = None,
        http_methods_to_capture: Optional[Iterable[str]] = None,
    ) -> None:
        if not _STARLETTE_AVAILABLE:
            raise ImportError(
                "AllStakFastAPI requires starlette/fastapi. "
                "Install with: pip install allstak[fastapi]"
            )
        self.app = app
        self.service = service
        self.transaction_style = (
            transaction_style if transaction_style in ("url", "endpoint") else "url"
        )
        self.failed_request_status_codes = _normalize_status_codes(
            failed_request_status_codes
        )
        self.http_methods_to_capture = _normalize_methods(http_methods_to_capture)

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = (scope.get("method") or "GET").upper()
        raw_path: str = scope.get("path", "/") or "/"

        # Methods like OPTIONS / HEAD are pass-through: no span, no telemetry,
        # but still fully fail-open — never touch the response.
        if method not in self.http_methods_to_capture:
            await self.app(scope, receive, send)
            return

        # Lazy import — the SDK might not be initialized.
        import allstak

        client = allstak.get_client()

        start_ms = time.monotonic() * 1000
        start_ts = _now_iso()

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        incoming_trace = headers.get("x-allstak-trace-id") or headers.get(
            "x-request-id"
        )
        request_id = (
            headers.get("x-request-id")
            or headers.get("x-allstak-request-id")
            or uuid.uuid4().hex
        )
        trace_id: str
        span = None
        if client is not None:
            try:
                if incoming_trace:
                    client.set_trace_id(incoming_trace)
                    trace_id = incoming_trace
                else:
                    client.reset_trace()  # fresh per-request trace
                    trace_id = client.get_trace_id()
                if self.service:
                    client.tracing.set_service(self.service)
                span = client.start_span(
                    "http.server",
                    description=f"{method} {raw_path}",
                    tags={
                        "http.method": method,
                        # Provisional route tag; replaced with the matched
                        # template once routing resolves.
                        "http.route": raw_path,
                    },
                )
            except Exception:
                trace_id = incoming_trace or uuid.uuid4().hex
                span = None
        else:
            trace_id = incoming_trace or uuid.uuid4().hex

        server = scope.get("server") or ("localhost", None)
        host_header = headers.get("host") or (
            f"{server[0]}:{server[1]}" if server[1] else server[0]
        )
        req_content_length = int(headers.get("content-length") or 0)

        # Mutable boxes shared with the send wrapper. ``finished`` flips true
        # only after the final body chunk so streaming spans close at the right
        # time.
        status_code_box = {"code": 0, "size": 0}
        response_state = {"started": False, "finished": False}

        async def send_wrapper(message: "Message") -> None:
            msg_type = message.get("type")
            if msg_type == "http.response.start":
                response_state["started"] = True
                status_code_box["code"] = int(message.get("status", 0))
                try:
                    message["headers"] = set_asgi_headers(
                        message.get("headers", []),
                        trace_id=trace_id,
                        request_id=request_id,
                        span_id=getattr(span, "span_id", None),
                        sampled=getattr(span, "sampled", True),
                    )
                except Exception:
                    pass
            elif msg_type == "http.response.body":
                body = message.get("body") or b""
                if body:
                    status_code_box["size"] += len(body)
                # Streaming responses arrive as multiple body chunks; the last
                # one has ``more_body`` false (or absent). Only then is the
                # response truly complete.
                if not message.get("more_body", False):
                    response_state["finished"] = True
            await send(message)

        exc_to_capture: Optional[BaseException] = None
        # Starlette populates scope["state"] lazily — ensure a dict so a
        # downstream handler can attach ``request.state.user``.
        if "state" not in scope:
            scope["state"] = {}

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            exc_to_capture = exc
            # No response was sent by an inner exception handler → the ASGI
            # server will turn this into a 500. Re-raise so the framework's own
            # error handling runs; swallowing would hang the connection.
            if not response_state["started"]:
                status_code_box["code"] = status_code_box["code"] or 500
            raise
        finally:
            self._finalize(
                client=client,
                span=span,
                scope=scope,
                method=method,
                raw_path=raw_path,
                host_header=host_header,
                headers=headers,
                status_code_box=status_code_box,
                req_content_length=req_content_length,
                start_ms=start_ms,
                start_ts=start_ts,
                trace_id=trace_id,
                request_id=request_id,
                exc_to_capture=exc_to_capture,
            )

    # ------------------------------------------------------------------
    # Finalization — telemetry, span close, error capture. Fully fail-open.
    # ------------------------------------------------------------------
    def _finalize(
        self,
        *,
        client: Any,
        span: Any,
        scope: "Scope",
        method: str,
        raw_path: str,
        host_header: str,
        headers: dict,
        status_code_box: dict,
        req_content_length: int,
        start_ms: float,
        start_ts: str,
        trace_id: str,
        request_id: str,
        exc_to_capture: Optional[BaseException],
    ) -> None:
        duration_ms = int(time.monotonic() * 1000 - start_ms)
        status_code = status_code_box["code"] or (500 if exc_to_capture else 0)

        # Low-cardinality name from the matched route template (or endpoint).
        txn_name = _transaction_name(scope, self.transaction_style)

        # A response is an error if its status code is in the configured set,
        # OR an exception bubbled out (treated as 500). This is status-code
        # driven: HTTPException(404) → 404 → not in the 5xx set → not reported;
        # HTTPException(503) → 503 → reported; unhandled error → 500 → reported.
        is_error = (status_code in self.failed_request_status_codes) or (
            exc_to_capture is not None
            and (status_code or 500) in self.failed_request_status_codes
        )

        if client is None:
            return

        # 1. HTTP telemetry.
        try:
            client.http.record(
                direction="inbound",
                method=method,
                host=host_header,
                path=raw_path,
                status_code=status_code or 0,
                duration_ms=duration_ms,
                request_size=req_content_length,
                response_size=status_code_box["size"],
                trace_id=trace_id,
                request_id=request_id,
                span_id=getattr(span, "span_id", None),
                timestamp=start_ts,
            )
        except Exception:
            pass

        # 2. User context from request.state.user (set by an auth dependency).
        try:
            user_kwargs = _extract_user(scope)
            if user_kwargs:
                client.set_user(**user_kwargs)
        except Exception:
            pass

        # 3. Close the span — named by the route template, tagged with status.
        if span is not None:
            try:
                span.set_tag("http.route", txn_name)
                span.set_tag("http.status_code", str(status_code or 0))
                span.set_tag("transaction", txn_name)
                span.finish("error" if is_error else "ok")
            except Exception:
                pass

        # 4. Capture server errors (5xx), whether raised or returned as a 5xx
        #    response. 4xx client errors are deliberately not reported.
        if is_error:
            try:
                from ..models.errors import RequestContext

                req_ctx = RequestContext(
                    method=method,
                    path=txn_name,
                    host=host_header,
                    status_code=status_code or 500,
                    user_agent=headers.get("user-agent"),
                )
                if exc_to_capture is not None:
                    client.capture_exception(
                        exc_to_capture,
                        request_context=req_ctx,
                        metadata=self._error_metadata(
                            method, raw_path, txn_name, host_header,
                            status_code or 500, trace_id, request_id,
                        ),
                        mechanism={"type": _MECHANISM_TYPE, "handled": True},
                    )
                else:
                    # 5xx response with no propagated exception (e.g.
                    # HTTPException(503) handled by the framework).
                    client.capture_error(
                        "HTTPServerError",
                        f"{method} {txn_name} responded with {status_code}",
                        level="error",
                        metadata=self._error_metadata(
                            method, raw_path, txn_name, host_header,
                            status_code, trace_id, request_id,
                        ),
                    )
            except Exception:
                pass

    @staticmethod
    def _error_metadata(
        method: str,
        raw_path: str,
        txn_name: str,
        host_header: str,
        status_code: int,
        trace_id: str,
        request_id: str,
    ) -> dict:
        return {
            "http.method": method,
            "http.path": raw_path,
            "http.route": txn_name,
            "http.host": host_header,
            "http.status": status_code,
            "traceId": trace_id,
            "requestId": request_id,
        }


class AllStakFastAPI:
    """
    Convenience wrapper that registers :class:`AllStakASGIMiddleware` on a
    FastAPI / Starlette app.

    Usage::

        from fastapi import FastAPI
        from allstak.integrations.fastapi import AllStakFastAPI

        app = FastAPI()
        AllStakFastAPI(app, service="my-api")

    All keyword options of :class:`AllStakASGIMiddleware`
    (``transaction_style``, ``failed_request_status_codes``,
    ``http_methods_to_capture``) are forwarded.
    """

    def __init__(
        self,
        app: Any,
        *,
        service: str = "",
        transaction_style: str = "url",
        failed_request_status_codes: Optional[Iterable[int]] = None,
        http_methods_to_capture: Optional[Iterable[str]] = None,
    ) -> None:
        if not _STARLETTE_AVAILABLE:
            raise ImportError(
                "AllStakFastAPI requires starlette/fastapi. "
                "Install with: pip install allstak[fastapi]"
            )
        app.add_middleware(
            AllStakASGIMiddleware,
            service=service,
            transaction_style=transaction_style,
            failed_request_status_codes=failed_request_status_codes,
            http_methods_to_capture=http_methods_to_capture,
        )
