"""
Flask extension integration for AllStak.

Records every inbound HTTP request/response automatically.

Setup::

    from flask import Flask
    from allstak.integrations.flask import AllStakFlask

    app = Flask(__name__)
    AllStakFlask(app)

Or use the factory pattern::

    allstak_ext = AllStakFlask()
    allstak_ext.init_app(app)
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Optional

from ..models.errors import RequestContext
from ..propagation import set_mapping_headers

try:
    import flask
    _FLASK_AVAILABLE = True
except ImportError:
    _FLASK_AVAILABLE = False


class AllStakFlask:
    """Flask extension that records inbound HTTP request telemetry."""

    def __init__(self, app: Optional[object] = None) -> None:
        if not _FLASK_AVAILABLE:
            raise ImportError("Flask is not installed")
        if app is not None:
            self.init_app(app)

    def init_app(self, app: object) -> None:
        """Register before/after request hooks on *app*."""
        app.before_request(self._before_request)  # type: ignore[attr-defined]
        app.after_request(self._after_request)    # type: ignore[attr-defined]
        app.teardown_request(self._teardown_request)  # type: ignore[attr-defined]

    @staticmethod
    def _before_request() -> None:
        flask.g._allstak_start_ms = time.monotonic() * 1000
        flask.g._allstak_start_ts = AllStakFlask._now_iso()
        try:
            import allstak
            client = allstak.get_client()
            if client:
                trace_id = _trace_id_from_headers(flask.request.headers) or uuid.uuid4().hex
                request_id = (
                    flask.request.headers.get("x-request-id")
                    or flask.request.headers.get("x-allstak-request-id")
                    or uuid.uuid4().hex
                )
                client.set_trace_id(trace_id)
                span = client.start_span(
                    "http.server",
                    description=f"{flask.request.method} {flask.request.path}",
                    tags={
                        "http.method": flask.request.method,
                        "http.route": flask.request.path,
                    },
                )
                flask.g._allstak_trace_id = trace_id
                flask.g._allstak_request_id = request_id
                flask.g._allstak_span = span
        except Exception:
            pass

    @staticmethod
    def _after_request(response: object) -> object:
        try:
            import allstak
            client = allstak.get_client()
            if client:
                start_ms = getattr(flask.g, "_allstak_start_ms", None)
                start_ts = getattr(flask.g, "_allstak_start_ts", AllStakFlask._now_iso())
                duration = int(time.monotonic() * 1000 - start_ms) if start_ms else 0

                req = flask.request
                path = req.path
                body_len = req.content_length or 0
                resp_len = (
                    int(response.headers.get("Content-Length", 0))  # type: ignore
                    if hasattr(response, "headers")
                    else 0
                )
                status = getattr(response, "status_code", 0)

                client.http.record(
                    direction="inbound",
                    method=req.method,
                    host=req.host or "localhost",
                    path=path,
                    status_code=status,
                    duration_ms=duration,
                    request_size=body_len,
                    response_size=resp_len,
                    timestamp=start_ts,
                    trace_id=getattr(flask.g, "_allstak_trace_id", None),
                    request_id=getattr(flask.g, "_allstak_request_id", None),
                    span_id=getattr(getattr(flask.g, "_allstak_span", None), "span_id", None),
                )
                span = getattr(flask.g, "_allstak_span", None)
                if span is not None:
                    span.set_tag("http.status_code", str(status))
                    span.finish("error" if status >= 500 else "ok")
                set_mapping_headers(
                    response.headers,  # type: ignore[attr-defined]
                    trace_id=getattr(flask.g, "_allstak_trace_id", None),
                    request_id=getattr(flask.g, "_allstak_request_id", None),
                    span_id=getattr(getattr(flask.g, "_allstak_span", None), "span_id", None),
                    sampled=getattr(getattr(flask.g, "_allstak_span", None), "sampled", True),
                )
        except Exception:
            pass
        return response

    @staticmethod
    def _teardown_request(exc: Optional[BaseException]) -> None:
        if exc is None:
            return
        try:
            import allstak
            client = allstak.get_client()
            if client:
                req = flask.request
                client.capture_exception(
                    exc,
                    request_context=RequestContext(
                        method=req.method,
                        path=req.path,
                        host=req.host,
                        user_agent=req.headers.get("user-agent"),
                    ),
                    metadata={
                        "traceId": getattr(flask.g, "_allstak_trace_id", None),
                        "requestId": getattr(flask.g, "_allstak_request_id", None),
                    },
                )
            span = getattr(flask.g, "_allstak_span", None)
            if span is not None:
                span.finish("error")
        except Exception:
            pass

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _trace_id_from_headers(headers: object) -> Optional[str]:
    traceparent = headers.get("traceparent") if hasattr(headers, "get") else None
    if traceparent:
        parts = str(traceparent).split("-")
        if len(parts) >= 2 and len(parts[1]) == 32:
            return parts[1]
    for name in ("x-allstak-trace-id", "x-trace-id"):
        value = headers.get(name) if hasattr(headers, "get") else None
        if value:
            return str(value)
    return None
