"""
Django middleware integration for AllStak.

Records every inbound HTTP request/response automatically.

Setup in settings.py::

    MIDDLEWARE = [
        "allstak.integrations.django.AllStakMiddleware",
        # ... your other middleware
    ]

    ALLSTAK = {
        "api_key": "ask_live_...",
        "host": "http://localhost:8080",
        "environment": "production",
    }

Or configure programmatically before the first request.
"""

from __future__ import annotations

import time
import uuid
from typing import Callable, Optional

from ..models.errors import RequestContext
from ..propagation import set_mapping_headers

try:
    from django.conf import settings
    from django.http import HttpRequest, HttpResponse
    _DJANGO_AVAILABLE = True
except ImportError:
    _DJANGO_AVAILABLE = False


class AllStakMiddleware:
    """
    Django middleware that tracks every inbound HTTP request.

    Automatically initializes the AllStak SDK on first use if
    ``ALLSTAK`` settings block is present.
    """

    def __init__(self, get_response: Callable) -> None:
        if not _DJANGO_AVAILABLE:
            raise ImportError("Django is not installed")
        self.get_response = get_response
        self._ensure_initialized()

    def __call__(self, request: "HttpRequest") -> "HttpResponse":
        start_ms = time.monotonic() * 1000
        start_ts = self._now_iso()
        trace_id = _trace_id_from_meta(request.META) or uuid.uuid4().hex
        request_id = (
            request.META.get("HTTP_X_REQUEST_ID")
            or request.META.get("HTTP_X_ALLSTAK_REQUEST_ID")
            or uuid.uuid4().hex
        )
        span = None
        client = None

        try:
            import allstak
            client = allstak.get_client()
            if client:
                client.set_trace_id(trace_id)
                span = client.start_span(
                    "http.server",
                    description=f"{request.method} {request.path}",
                    tags={
                        "http.method": request.method or "GET",
                        "http.route": request.path,
                    },
                )
        except Exception:
            client = None

        response: Optional["HttpResponse"] = None
        error: Optional[BaseException] = None
        try:
            response = self.get_response(request)
            if response is not None:
                set_mapping_headers(
                    response,
                    trace_id=trace_id,
                    request_id=request_id,
                    span_id=getattr(span, "span_id", None),
                )
            return response
        except BaseException as exc:
            error = exc
            if client:
                try:
                    client.capture_exception(
                        exc,
                        request_context=RequestContext(
                            method=request.method or "GET",
                            path=request.path,
                            host=request.get_host(),
                            user_agent=request.META.get("HTTP_USER_AGENT"),
                        ),
                        metadata={"traceId": trace_id, "requestId": request_id},
                    )
                except Exception:
                    pass
            raise
        finally:
            status_code = response.status_code if response is not None else 500

            try:
                if client:
                    duration = int(time.monotonic() * 1000 - start_ms)
                    path = request.path  # already without query string
                    body_len = int(request.META.get("CONTENT_LENGTH") or 0)
                    resp_len = (
                        len(response.content)
                        if response is not None and hasattr(response, "content")
                        else 0
                    )

                    client.http.record(
                        direction="inbound",
                        method=request.method or "GET",
                        host=request.get_host() or "localhost",
                        path=path,
                        status_code=status_code,
                        duration_ms=duration,
                        request_size=body_len,
                        response_size=resp_len,
                        timestamp=start_ts,
                        trace_id=trace_id,
                        request_id=request_id,
                        span_id=getattr(span, "span_id", None),
                        error_fingerprint=type(error).__name__ if error else None,
                    )
                if span is not None:
                    span.set_tag("http.status_code", str(status_code))
                    span.finish("error" if status_code >= 500 or error else "ok")
            except Exception:
                pass

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _ensure_initialized() -> None:
        try:
            import allstak
            if allstak.get_client() is not None:
                return
            cfg = getattr(settings, "ALLSTAK", {})
            if cfg.get("api_key"):
                allstak.init(**cfg)
        except Exception:
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
