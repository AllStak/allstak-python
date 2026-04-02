"""
AllStak Python SDK

Observability, error tracking, logging, HTTP monitoring,
session replay, cron monitoring, and feature flags for Python applications.

Quick start::

    import allstak

    allstak.init(api_key="ask_live_...", host="http://localhost:8080")

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
from .models.errors import UserContext
from .models.logs import LOG_LEVELS
from .models.http_requests import HttpRequestItem
from .models.replay import ReplayEvent, ReplayPayload
from .models.heartbeat import HeartbeatPayload

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # Init
    "init",
    "get_client",
    # Config
    "AllStakConfig",
    "AllStakClient",
    # Models
    "UserContext",
    "HttpRequestItem",
    "ReplayEvent",
    "ReplayPayload",
    "HeartbeatPayload",
    # Module-level shortcuts
    "capture_exception",
    "capture_error",
    "set_user",
    "clear_user",
    "flush",
    "log",
    "http",
    "replay",
    "cron",
    "flags",
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
            def _noop(*args: Any, **kwargs: Any) -> None:
                pass
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


# Singleton proxy instances
log = _LogProxy()
http = _HttpProxy()
replay = _ReplayProxy()
cron = _CronProxy()
flags = _FlagsProxy()
