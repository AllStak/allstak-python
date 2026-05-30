"""
AllStak Python SDK

Observability, error tracking, logging, HTTP monitoring,
session replay, cron monitoring, and feature flags for Python applications.

Quick start::

    import allstak

    allstak.init(api_key="ask_live_...")

    # Capture exceptions
    try:
        risky()
    except Exception as e:
        allstak.capture_exception(e)

    # Logs
    allstak.log.info("Hello from AllStak!")

    # HTTP monitoring
    allstak.http.record(
        direction="outbound",
        method="GET",
        host="api.example.com",
        path="/v1/data",
        status_code=200,
        duration_ms=142,
    )

    # Cron jobs
    with allstak.cron.job("my-job-slug"):
        run_job()

    # Flush on shutdown
    allstak.flush()
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import (
    AllStakClient,
    _require_client,
    get_client,
    init,
)
from .config import AllStakConfig
from .integrations.logging import AllStakLoggingHandler, install_logging
from .spool import EventSpool
from .models.breadcrumb import Breadcrumb
from .models.errors import RequestContext, UserContext
from .models.logs import LOG_LEVELS
from .models.http_requests import HttpRequestItem
from .models.replay import ReplayEvent, ReplayPayload
from .models.heartbeat import HeartbeatPayload
from .modules.tracing import Span, TracingModule

__version__ = "0.2.0"

__all__ = [
    "__version__",
    # Init
    "init",
    "get_client",
    # Config
    "AllStakConfig",
    "AllStakClient",
    # Offline persistence
    "EventSpool",
    # Models
    "Breadcrumb",
    "UserContext",
    "RequestContext",
    "HttpRequestItem",
    "ReplayEvent",
    "ReplayPayload",
    "HeartbeatPayload",
    "Span",
    "TracingModule",
    # Logging bridge (public — auto-attached by init when capture_logs=True)
    "install_logging",
    "AllStakLoggingHandler",
    # Module-level shortcuts
    "capture_exception",
    "capture_error",
    "add_breadcrumb",
    "clear_breadcrumbs",
    "set_user",
    "clear_user",
    "flush",
    "get_diagnostics",
    "log",
    "http",
    "replay",
    "cron",
    "flags",
    "tracing",
    "shutdown",
    "database",
    "start_span",
    "get_trace_id",
    "set_trace_id",
    "get_current_span_id",
    "reset_trace",
]


# ---------------------------------------------------------------------------
# Module-level proxy helpers
# These delegate to the singleton client.
# They are no-ops if init() has not been called — they never raise.
# ---------------------------------------------------------------------------

def capture_exception(
    exc: BaseException,
    *,
    level: str = "error",
    environment: Optional[str] = None,
    release: Optional[str] = None,
    session_id: Optional[str] = None,
    user: Optional[UserContext] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Capture a Python exception and send it to AllStak.

    Returns the event ID on success, None on failure.  Never raises.
    No-op if :func:`init` has not been called.
    """
    client = get_client()
    if client is None:
        return None
    return client.capture_exception(
        exc,
        level=level,
        environment=environment,
        release=release,
        session_id=session_id,
        user=user,
        metadata=metadata,
    )


def capture_error(
    exception_class: str,
    message: str,
    *,
    stack_trace: Optional[List[str]] = None,
    level: str = "error",
    environment: Optional[str] = None,
    release: Optional[str] = None,
    session_id: Optional[str] = None,
    user: Optional[UserContext] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Capture an error by name + message without a Python exception object.

    Returns the event ID on success, None on failure.  Never raises.
    No-op if :func:`init` has not been called.
    """
    client = get_client()
    if client is None:
        return None
    return client.capture_error(
        exception_class,
        message,
        stack_trace=stack_trace,
        level=level,
        environment=environment,
        release=release,
        session_id=session_id,
        user=user,
        metadata=metadata,
    )


def add_breadcrumb(
    type: str,
    message: str,
    level: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Add a breadcrumb to the internal buffer.

    Breadcrumbs are attached to the next captured error and then cleared.
    No-op if :func:`init` has not been called.
    """
    client = get_client()
    if client is not None:
        client.add_breadcrumb(type, message, level, data)


def clear_breadcrumbs() -> None:
    """Clear all breadcrumbs from the buffer."""
    client = get_client()
    if client is not None:
        client.clear_breadcrumbs()


def set_user(
    user_id: Optional[str] = None,
    email: Optional[str] = None,
    ip: Optional[str] = None,
) -> None:
    """Set the current user context for subsequent error events."""
    client = get_client()
    if client:
        client.set_user(user_id=user_id, email=email, ip=ip)


def clear_user() -> None:
    """Clear the current user context."""
    client = get_client()
    if client:
        client.clear_user()


def flush() -> None:
    """Flush all pending events synchronously."""
    client = get_client()
    if client:
        client.flush()


def get_diagnostics() -> Dict[str, Any]:
    """Return privacy-safe SDK diagnostic counters, or an empty snapshot before init."""
    client = get_client()
    if client:
        return client.get_diagnostics()
    return {}


def shutdown() -> None:
    """Flush all pending events and shut down background threads."""
    client = get_client()
    if client:
        client._shutdown()


# ---------------------------------------------------------------------------
# Module-level proxy properties
# These return the module objects from the singleton client.
# ---------------------------------------------------------------------------

class _LogProxy:
    """Proxy to the LogModule on the singleton client."""

    def __getattr__(self, name: str) -> Any:
        client = get_client()
        if client is None:
            # Return a no-op callable
            def _noop(*args: Any, **kwargs: Any) -> None:
                pass
            return _noop
        return getattr(client.log, name)


class _HttpProxy:
    """Proxy to the HttpMonitorModule on the singleton client."""

    def __getattr__(self, name: str) -> Any:
        client = get_client()
        if client is None:
            def _noop(*args: Any, **kwargs: Any) -> None:
                pass
            return _noop
        return getattr(client.http, name)


class _ReplayProxy:
    """Proxy to the ReplayModule on the singleton client."""

    def __getattr__(self, name: str) -> Any:
        client = get_client()
        if client is None:
            def _noop(*args: Any, **kwargs: Any) -> None:
                pass
            return _noop
        return getattr(client.replay, name)


class _CronProxy:
    """Proxy to the CronModule on the singleton client."""

    def __getattr__(self, name: str) -> Any:
        client = get_client()
        if client is None:
            if name == "job":
                # Return a no-op context manager so `with allstak.cron.job(...):`
                # keeps working when the SDK is not initialized.
                from contextlib import contextmanager

                @contextmanager
                def _noop_job(*args: Any, **kwargs: Any) -> Any:
                    yield None
                return _noop_job

            def _noop(*args: Any, **kwargs: Any) -> None:
                return None
            return _noop
        return getattr(client.cron, name)


class _FlagsProxy:
    """Proxy to the FeatureFlagModule on the singleton client."""

    def __getattr__(self, name: str) -> Any:
        client = get_client()
        if client is None:
            def _noop(*args: Any, **kwargs: Any) -> None:
                pass
            return _noop
        return getattr(client.flags, name)


class _TracingProxy:
    """Proxy to the TracingModule on the singleton client."""

    def __getattr__(self, name: str) -> Any:
        client = get_client()
        if client is None:
            def _noop(*args: Any, **kwargs: Any) -> None:
                pass
            return _noop
        return getattr(client.tracing, name)


class _DatabaseProxy:
    """Proxy to the DatabaseModule on the singleton client."""

    def __getattr__(self, name: str) -> Any:
        client = get_client()
        if client is None:
            def _noop(*args: Any, **kwargs: Any) -> None:
                pass
            return _noop
        return getattr(client.database, name)


# Singleton proxy instances
log = _LogProxy()
http = _HttpProxy()
replay = _ReplayProxy()
cron = _CronProxy()
flags = _FlagsProxy()
tracing = _TracingProxy()
database = _DatabaseProxy()


# ---------------------------------------------------------------------------
# Module-level tracing shortcuts
# ---------------------------------------------------------------------------

def start_span(
    operation: str,
    *,
    description: str = "",
    tags: Optional[Dict[str, Any]] = None,
) -> "Span":
    """
    Start a new distributed tracing span.

    Can be used as a context manager::

        with allstak.start_span("db.query") as span:
            span.set_tag("db.type", "postgresql")
            result = db.execute(query)

    No-op if :func:`init` has not been called (returns a span that
    finishes silently).
    """
    client = get_client()
    if client is None:
        # Return a dummy span that does nothing
        from .modules.tracing import Span as _Span
        return _Span(
            trace_id="",
            span_id="",
            parent_span_id="",
            operation=operation,
            description=description,
            service="",
            environment="",
            tags=tags or {},
            start_time_millis=0,
            on_finish=lambda s: None,
        )
    return client.start_span(operation, description=description, tags=tags)


def get_trace_id() -> str:
    """Get the current trace ID (creates one if none exists)."""
    client = get_client()
    if client is None:
        return ""
    return client.get_trace_id()


def set_trace_id(trace_id: str) -> None:
    """Set the trace ID explicitly (e.g. from an incoming request header)."""
    client = get_client()
    if client is not None:
        client.set_trace_id(trace_id)


def get_current_span_id() -> Optional[str]:
    """Get the current active span ID, or None."""
    client = get_client()
    if client is None:
        return None
    return client.get_current_span_id()


def reset_trace() -> None:
    """Reset trace context (trace ID and span stack)."""
    client = get_client()
    if client is not None:
        client.reset_trace()
