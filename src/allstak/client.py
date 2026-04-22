"""
AllStakClient — the main entry point for the AllStak Python SDK.

Usage::

    import allstak

    allstak.init(api_key="ask_live_...", host="http://localhost:8080")

    # Capture an exception
    try:
        risky_operation()
    except Exception as e:
        allstak.capture_exception(e)

    # Log a message
    allstak.log.info("Order placed", metadata={"order_id": "ORD-1234"})

    # Record an outbound HTTP call
    allstak.http.record(
        direction="outbound",
        method="POST",
        host="payments.stripe.com",
        path="/v1/charges",
        status_code=200,
        duration_ms=320,
    )

    # Cron heartbeat
    with allstak.cron.job("daily-report"):
        run_daily_report()

    # Flush everything before shutdown
    allstak.flush()
"""

from __future__ import annotations

import atexit
import logging
import sys
import threading
from typing import Any, Dict, List, Optional

from .config import AllStakConfig
from .models.errors import RequestContext, UserContext
from .modules.cron import CronModule, JobHandle
from .modules.database import DatabaseModule, enable_db_auto_instrumentation
from .modules.errors import ErrorModule
from .modules.flags import FeatureFlagModule
from .modules.http_monitor import HttpMonitorModule
from .modules.logs import LogModule
from .modules.replay import ReplayModule, ReplaySession
from .modules.tracing import Span, TracingModule
from .transport import AllStakAuthError, HttpTransport

logger = logging.getLogger("allstak.sdk")


class AllStakClient:
    """
    The AllStak Python SDK client.

    Instantiate once at application startup via ``AllStakClient(config)``
    or use the module-level helpers after calling ``allstak.init()``.
    """

    def __init__(self, config: AllStakConfig) -> None:
        self._config = config
        self._initialized = True
        self._disabled = False
        self._lock = threading.Lock()

        if config.debug:
            logging.basicConfig(
                level=logging.DEBUG,
                format="%(asctime)s %(name)s %(levelname)s %(message)s",
                stream=sys.stderr,
            )

        self._transport = HttpTransport(
            api_key=config.api_key,
            host=config.host,
            connect_timeout=config.connect_timeout,
            read_timeout=config.read_timeout,
            max_retries=config.max_retries,
            debug=config.debug,
        )

        # Feature modules
        self._errors = ErrorModule(self._transport, config)
        self._logs = LogModule(self._transport, config)
        self._http = HttpMonitorModule(self._transport, config)
        self._replay = ReplayModule(self._transport, config)
        self._cron = CronModule(self._transport, config)
        self._flags = FeatureFlagModule(config)
        self._tracing = TracingModule(self._transport, config)
        self._database = DatabaseModule(self._transport, config)

        # Best-effort flush on interpreter exit
        atexit.register(self._shutdown)

        # Wire automatic breadcrumb instrumentation
        if config.auto_breadcrumbs:
            try:
                from .integrations.auto_breadcrumbs import instrument_requests, instrument_logging
                instrument_requests(self.add_breadcrumb)
                instrument_logging(self.add_breadcrumb)
                self._logs.set_on_log_breadcrumb(self.add_breadcrumb)
            except Exception as e:
                logger.debug("[AllStak] auto-breadcrumb instrumentation failed: %s", e)

        # Wire automatic database instrumentation
        try:
            enable_db_auto_instrumentation(self._database)
        except Exception as e:
            logger.debug("[AllStak] DB auto-instrumentation failed: %s", e)

        logger.debug("[AllStak] SDK initialized (host=%s, debug=%s)", config.host, config.debug)

    # ------------------------------------------------------------------
    # Module accessors
    # ------------------------------------------------------------------

    @property
    def log(self) -> LogModule:
        """Log module — ``allstak.log.info()``, ``allstak.log.error()``, etc."""
        return self._logs

    @property
    def http(self) -> HttpMonitorModule:
        """HTTP monitoring module — ``allstak.http.record()``."""
        return self._http

    @property
    def replay(self) -> ReplayModule:
        """Session replay module — ``allstak.replay.start_session()``."""
        return self._replay

    @property
    def cron(self) -> CronModule:
        """Cron monitoring module — ``allstak.cron.job()``."""
        return self._cron

    @property
    def flags(self) -> FeatureFlagModule:
        """Feature flags module — ``allstak.flags.get()``."""
        return self._flags

    @property
    def tracing(self) -> TracingModule:
        """Tracing module — ``allstak.tracing.start_span()``."""
        return self._tracing

    @property
    def database(self) -> DatabaseModule:
        """Database monitoring module — ``allstak.database.record()``."""
        return self._database

    # ------------------------------------------------------------------
    # Distributed Tracing
    # ------------------------------------------------------------------

    def start_span(
        self,
        operation: str,
        *,
        description: str = "",
        tags: Optional[Dict[str, Any]] = None,
    ) -> Span:
        """
        Start a new span. Automatically parented to the current active span.

        Can be used as a context manager::

            with allstak.start_span("db.query", description="SELECT users") as span:
                span.set_tag("db.type", "postgresql")
                result = db.execute(query)

        Returns the Span object.
        """
        if self._disabled:
            # Return a no-op span that does nothing
            return self._tracing.start_span(operation, description=description, tags=tags)
        return self._tracing.start_span(operation, description=description, tags=tags)

    def get_trace_id(self) -> str:
        """Get the current trace ID (creates one if none exists)."""
        return self._tracing.get_trace_id()

    def set_trace_id(self, trace_id: str) -> None:
        """Set the trace ID explicitly (e.g. from an incoming request header)."""
        self._tracing.set_trace_id(trace_id)

    def get_current_span_id(self) -> Optional[str]:
        """Get the current active span ID, or None if no span is active."""
        return self._tracing.get_current_span_id()

    def reset_trace(self) -> None:
        """Reset trace context (trace ID and span stack)."""
        self._tracing.reset_trace()

    # ------------------------------------------------------------------
    # Error capture
    # ------------------------------------------------------------------

    def capture_exception(
        self,
        exc: BaseException,
        *,
        level: str = "error",
        environment: Optional[str] = None,
        release: Optional[str] = None,
        session_id: Optional[str] = None,
        user: Optional[UserContext] = None,
        request_context: Optional[RequestContext] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Capture a Python exception and send it to AllStak.

        Returns the event ID on success, None on failure.  Never raises.

        :param exc: The exception to capture.
        :param level: Severity level (default ``"error"``).
        :param metadata: Arbitrary key-value metadata.
        """
        if self._disabled:
            return None
        try:
            # Auto-attach trace context to error metadata
            enriched_meta = dict(metadata) if metadata else {}
            trace_id = self._tracing.get_trace_id()
            span_id = self._tracing.get_current_span_id()
            if trace_id and "traceId" not in enriched_meta:
                enriched_meta["traceId"] = trace_id
            if span_id and "spanId" not in enriched_meta:
                enriched_meta["spanId"] = span_id

            return self._errors.capture_exception(
                exc,
                level=level,
                environment=environment,
                release=release,
                session_id=session_id,
                user=user,
                request_context=request_context,
                trace_id=trace_id or None,
                metadata=enriched_meta if enriched_meta else None,
            )
        except AllStakAuthError:
            self._handle_auth_error()
            return None
        except Exception as e:
            logger.debug("[AllStak] capture_exception swallowed: %s", e)
            return None

    def capture_error(
        self,
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
        Capture an error by class name + message without a Python exception.

        Useful for re-surfacing errors from external systems or logs.
        Returns the event ID on success, None on failure.  Never raises.
        """
        if self._disabled:
            return None
        try:
            # Auto-attach trace context to error metadata
            enriched_meta = dict(metadata) if metadata else {}
            trace_id = self._tracing.get_trace_id()
            span_id = self._tracing.get_current_span_id()
            if trace_id and "traceId" not in enriched_meta:
                enriched_meta["traceId"] = trace_id
            if span_id and "spanId" not in enriched_meta:
                enriched_meta["spanId"] = span_id

            return self._errors.capture_error(
                exception_class,
                message,
                stack_trace=stack_trace,
                level=level,
                environment=environment,
                release=release,
                session_id=session_id,
                user=user,
                metadata=enriched_meta if enriched_meta else None,
            )
        except AllStakAuthError:
            self._handle_auth_error()
            return None
        except Exception as e:
            logger.debug("[AllStak] capture_error swallowed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Breadcrumbs
    # ------------------------------------------------------------------

    def add_breadcrumb(
        self,
        type: str,
        message: str,
        level: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Add a breadcrumb to the internal ring buffer.

        Breadcrumbs are attached to the next captured error event
        and then cleared. Max 50 breadcrumbs are kept; oldest are dropped.

        :param type: Category ("http", "log", "ui", "navigation", "query", "default").
        :param message: Human-readable description.
        :param level: Severity ("info", "warn", "error", "debug"). Defaults to "info".
        :param data: Optional key-value metadata.
        """
        if self._disabled:
            return
        self._errors.add_breadcrumb(type, message, level, data)

    def clear_breadcrumbs(self) -> None:
        """Clear all breadcrumbs from the buffer."""
        self._errors.clear_breadcrumbs()

    # ------------------------------------------------------------------
    # User context
    # ------------------------------------------------------------------

    def set_user(
        self,
        user_id: Optional[str] = None,
        email: Optional[str] = None,
        ip: Optional[str] = None,
    ) -> None:
        """Set a default user context attached to all subsequent error events."""
        self._errors.set_user(UserContext(id=user_id, email=email, ip=ip))

    def clear_user(self) -> None:
        """Clear the current user context."""
        self._errors.clear_user()

    # ------------------------------------------------------------------
    # Flush & shutdown
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """Synchronously flush all pending events across all modules."""
        try:
            self._logs.flush()
            self._http.flush()
            self._replay.flush()
            self._tracing.flush()
            self._database.flush()
        except Exception as e:
            logger.debug("[AllStak] flush() error: %s", e)

    def _shutdown(self) -> None:
        """Called automatically at interpreter exit (via atexit)."""
        try:
            self._logs.shutdown()
            self._http.shutdown()
            self._replay.shutdown()
            self._tracing.shutdown()
            self._database.shutdown()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _handle_auth_error(self) -> None:
        self._disabled = True
        logger.warning(
            "[AllStak] SDK disabled: API key is invalid (401). "
            "No further events will be sent this session."
        )


# ---------------------------------------------------------------------------
# Module-level API (singleton pattern)
# ---------------------------------------------------------------------------

_client: Optional[AllStakClient] = None
_init_lock = threading.Lock()
_initialized_once = False


def init(
    api_key: Optional[str] = None,
    host: str = "https://api.allstak.sa",
    *,
    environment: Optional[str] = None,
    release: Optional[str] = None,
    flush_interval_ms: int = 5_000,
    buffer_size: int = 500,
    debug: bool = False,
    connect_timeout: float = 3.0,
    read_timeout: float = 3.0,
    max_retries: int = 5,
    auto_breadcrumbs: bool = True,
    max_breadcrumbs: int = 50,
) -> AllStakClient:
    """
    Initialize the AllStak SDK.

    Call once at application startup.  Subsequent calls are no-ops
    and return the existing client.

    :param api_key: Raw API key (``X-AllStak-Key``).
                    Falls back to ``ALLSTAK_API_KEY`` env var.
    :param host: AllStak backend URL (default: ``https://api.allstak.sa``).
    :param environment: Deployment environment (e.g. ``"production"``).
    :param release: App version / release tag (e.g. ``"v1.4.2"``).
    :param flush_interval_ms: Background flush interval in milliseconds.
    :param buffer_size: Max buffered items per feature before eviction.
    :param debug: Enable verbose SDK debug logging to stderr.
    """
    global _client, _initialized_once

    with _init_lock:
        if _initialized_once and _client is not None:
            logger.warning(
                "[AllStak] init() called more than once — ignoring. "
                "Use allstak.get_client() to access the existing client."
            )
            return _client

        import os

        resolved_key = api_key or os.environ.get("ALLSTAK_API_KEY", "")
        resolved_host = host or os.environ.get("ALLSTAK_HOST", "https://api.allstak.sa")
        resolved_env = environment or os.environ.get("ALLSTAK_ENVIRONMENT")
        resolved_release = release or os.environ.get("ALLSTAK_RELEASE")

        config = AllStakConfig(
            api_key=resolved_key,
            host=resolved_host,
            environment=resolved_env,
            release=resolved_release,
            flush_interval_ms=flush_interval_ms,
            buffer_size=buffer_size,
            debug=debug,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            max_retries=max_retries,
            auto_breadcrumbs=auto_breadcrumbs,
            max_breadcrumbs=max_breadcrumbs,
        )
        _client = AllStakClient(config)
        _initialized_once = True

        # Best-effort auto-instrumentation of common outbound HTTP libraries.
        # Each integration is a no-op if its underlying lib isn't installed.
        try:
            from .integrations.httpx import install_httpx
            install_httpx()
        except Exception as e:  # pragma: no cover — never fail init
            logger.debug("[AllStak] httpx auto-install failed: %s", e)

        return _client


def get_client() -> Optional[AllStakClient]:
    """Return the initialized client, or None if ``init()`` has not been called."""
    return _client


def _require_client() -> AllStakClient:
    if _client is None:
        raise RuntimeError(
            "AllStak SDK is not initialized. Call allstak.init(api_key=...) first."
        )
    return _client
