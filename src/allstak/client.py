"""
AllStakClient — the main entry point for the AllStak Python SDK.

Usage::

    import allstak

    allstak.init(api_key="ask_live_...")

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
import os
import sys
import threading
from typing import Any, Callable, Dict, List, Optional

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

        # Offline/persistent event queue. Attach the spool to the transport so
        # undeliverable payloads are persisted (PII-scrubbed) instead of dropped,
        # then asynchronously replay anything left from a previous run. Entirely
        # fail-open and a no-op when opted out or the spool dir is unwritable.
        self._spool = None
        if config.offline_storage:
            try:
                from .spool import EventSpool, default_spool_dir

                directory = config.offline_queue_dir or default_spool_dir(config.host)
                spool = EventSpool(
                    directory,
                    max_events=config.offline_max_events,
                    max_bytes=config.offline_max_bytes,
                    max_age_s=config.offline_max_age_s,
                    enabled=True,
                )
                self._transport.set_spool(spool)
                self._spool = spool
            except Exception as e:  # pragma: no cover — never fail init
                logger.debug("[AllStak] offline spool init failed: %s", e)

        self._register_runtime_release()
        self._drain_offline_spool()

        # Feature modules
        self._errors = ErrorModule(self._transport, config)
        self._logs = LogModule(self._transport, config)
        self._http = HttpMonitorModule(self._transport, config)
        self._replay = ReplayModule(self._transport, config)
        self._cron = CronModule(self._transport, config)
        self._flags = FeatureFlagModule(config)
        self._tracing = TracingModule(self._transport, config)
        self._database = DatabaseModule(self._transport, config)

        # Release-health: open one session for this process (standard
        # "one session per process"). Skipped under a unit-test runtime
        # (mirrors the release-registration guard) and when opted out via
        # config.enable_auto_session_tracking. Fully fail-open.
        self._session_tracker = None
        if config.enable_auto_session_tracking and not self._is_test_runtime():
            try:
                from .session import SessionTracker

                tracker = SessionTracker(self._transport, config)
                # Let the session carry the configured/default user id when set.
                tracker._user_id_getter = self._current_user_id
                tracker.start()
                self._session_tracker = tracker
            except Exception as e:  # pragma: no cover — never fail init
                logger.debug("[AllStak] session tracking start failed: %s", e)

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

        # Install global uncaught-exception hooks (idempotent). Captures
        # exceptions that escape all application code outside a request.
        if config.install_excepthook or config.install_threading_excepthook:
            try:
                from .excepthook import install as _install_excepthook
                _install_excepthook(
                    get_client,
                    install_sys=config.install_excepthook,
                    install_threading=config.install_threading_excepthook,
                )
            except Exception as e:
                logger.debug("[AllStak] excepthook install failed: %s", e)

        logger.debug("[AllStak] SDK initialized (host=%s, debug=%s)", config.host, config.debug)

    @staticmethod
    def _is_test_runtime() -> bool:
        """Whether we appear to be running under a unit-test harness.

        Mirrors the release-registration guard so session tracking does not
        POST ``/ingest/v1/sessions/*`` during the SDK's own test suite
        (matches the Java SDK's ``isLikelyTestRuntime`` idea).
        """
        return (
            "PYTEST_CURRENT_TEST" in os.environ
            or os.environ.get("PYTHON_ENV") == "test"
            or "pytest" in os.path.basename(sys.argv[0])
            or "unittest" in os.path.basename(sys.argv[0])
        )

    def _current_user_id(self) -> Optional[str]:
        """Return the configured/default user id, if a user context is set."""
        try:
            user = getattr(self._errors, "_current_user", None)
            return getattr(user, "id", None) if user is not None else None
        except Exception:
            return None

    def _register_runtime_release(self) -> None:
        if (
            not self._config.auto_register_release
            or not self._config.api_key
            or not self._config.release
            or self._is_test_runtime()
        ):
            return

        def worker() -> None:
            try:
                self._transport.post(
                    "/ingest/v1/releases",
                    {
                        "version": self._config.release,
                        "environment": self._config.environment or "production",
                        "commitSha": self._config.commit_sha,
                        "branch": self._config.branch,
                        "author": f"{self._config.sdk_name}/{self._config.sdk_version}",
                        "message": "Registered automatically by AllStak Python SDK at runtime",
                    },
                )
            except Exception:
                logger.debug("[AllStak] runtime release registration failed", exc_info=True)

        thread = threading.Thread(target=worker, name="allstak-release-registration", daemon=True)
        thread.start()

    def _drain_offline_spool(self) -> None:
        """Replay events persisted by a previous run, on a daemon thread.

        Fail-open and skipped under the SDK's own test runtime (mirrors release
        registration) so unit tests do not make network calls. Re-sends through
        the existing transport so retry/backoff/circuit-breaker still apply.
        """
        spool = getattr(self, "_spool", None)
        if spool is None or self._is_test_runtime():
            return
        try:
            spool.drain_async(self._transport.send_for_drain)
        except Exception as e:  # pragma: no cover — never fail init
            logger.debug("[AllStak] offline spool drain failed: %s", e)

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
        mechanism: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Capture a Python exception and send it to AllStak.

        Returns the event ID on success, None on failure.  Never raises.

        :param exc: The exception to capture.
        :param level: Severity level (default ``"error"``).
        :param metadata: Arbitrary key-value metadata.
        :param mechanism: How the event was captured, e.g.
            ``{"type": "excepthook", "handled": False}`` for unhandled errors.
        """
        if self._disabled:
            return None
        try:
            # Auto-attach trace context to error metadata. Release-tracking
            # tags (sdk.name/version, platform, dist, commit.sha/branch) are
            # merged in last so they always reach the wire.
            enriched_meta = dict(metadata) if metadata else {}
            for k, v in self._config.release_tags().items():
                enriched_meta.setdefault(k, v)
            trace_id = self._tracing.get_trace_id()
            span_id = self._tracing.get_current_span_id()
            if trace_id and "traceId" not in enriched_meta:
                enriched_meta["traceId"] = trace_id
            if span_id and "spanId" not in enriched_meta:
                enriched_meta["spanId"] = span_id

            # Attach the active release-health session id so the backend's
            # error consumer can mark the session errored/crashed. The caller
            # may override it explicitly.
            effective_session_id = session_id or self._active_session_id()

            event_id = self._errors.capture_exception(
                exc,
                level=level,
                environment=environment,
                release=release,
                session_id=effective_session_id,
                user=user,
                request_context=request_context,
                trace_id=trace_id or None,
                metadata=enriched_meta if enriched_meta else None,
                mechanism=mechanism,
            )
            # Local release-health status transition.
            self._record_session_status(level, mechanism)
            return event_id
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
            # Auto-attach trace context to error metadata. Release-tracking
            # tags (sdk.name/version, platform, dist, commit.sha/branch) are
            # merged in last so they always reach the wire.
            enriched_meta = dict(metadata) if metadata else {}
            for k, v in self._config.release_tags().items():
                enriched_meta.setdefault(k, v)
            trace_id = self._tracing.get_trace_id()
            span_id = self._tracing.get_current_span_id()
            if trace_id and "traceId" not in enriched_meta:
                enriched_meta["traceId"] = trace_id
            if span_id and "spanId" not in enriched_meta:
                enriched_meta["spanId"] = span_id

            # Attach the active release-health session id (caller may override).
            effective_session_id = session_id or self._active_session_id()

            event_id = self._errors.capture_error(
                exception_class,
                message,
                stack_trace=stack_trace,
                level=level,
                environment=environment,
                release=release,
                session_id=effective_session_id,
                user=user,
                metadata=enriched_meta if enriched_meta else None,
            )
            # Local release-health status transition (capture_error is always
            # a handled capture, so it can only escalate to errored).
            self._record_session_status(level, None)
            return event_id
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

    def _active_session_id(self) -> Optional[str]:
        """The current release-health session id, or None if not tracking."""
        tracker = getattr(self, "_session_tracker", None)
        if tracker is None:
            return None
        try:
            session = tracker.current()
            return session.id if session is not None else None
        except Exception:
            return None

    def _record_session_status(
        self, level: str, mechanism: Optional[Dict[str, Any]]
    ) -> None:
        """Transition the local session status for a captured event.

        An UNHANDLED capture (``mechanism.handled is False``) or a ``fatal``
        level marks the session crashed; any other ``error``/``fatal`` capture
        marks it errored. Lower levels (warning/info) leave it unchanged.
        Never raises.
        """
        tracker = getattr(self, "_session_tracker", None)
        if tracker is None:
            return
        try:
            handled = True
            if mechanism is not None:
                handled = mechanism.get("handled", True)
            effective_level = (level or "error").lower()
            if handled is False or effective_level == "fatal":
                tracker.record_crash()
            elif effective_level == "error":
                tracker.record_error()
        except Exception as e:  # pragma: no cover — status update must not raise
            logger.debug("[AllStak] session status update failed: %s", e)

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
        # Close the release-health session last so the end POST reflects the
        # final accumulated status. Best-effort, never blocks or raises.
        tracker = getattr(self, "_session_tracker", None)
        if tracker is not None:
            try:
                tracker.end(None)
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
    install_excepthook: bool = True,
    install_threading_excepthook: bool = True,
    before_send: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None,
    sample_rate: float = 1.0,
    traces_sample_rate: Optional[float] = None,
    auto_register_release: bool = True,
    enable_auto_session_tracking: bool = True,
    offline_storage: bool = True,
    offline_queue_dir: Optional[str] = None,
    offline_max_events: int = 100,
    offline_max_bytes: int = 5 * 1024 * 1024,
    offline_max_age_s: float = 48 * 3600,
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
    :param install_excepthook: Install ``sys.excepthook`` to capture uncaught
        exceptions on the main thread. Default True.
    :param install_threading_excepthook: Install ``threading.excepthook`` to
        capture uncaught exceptions in background threads. Default True.
    :param before_send: Optional callback ``(event_dict) -> event_dict | None``
        run just before transport; return None to drop the event.
    :param sample_rate: Probabilistic error/message sample rate ``[0, 1]``.
    :param traces_sample_rate: Probabilistic span/transaction sample rate
        ``[0, 1]``; ``None`` keeps tracing always-on (backward compatible).
    :param auto_register_release: Register the resolved release at runtime
        startup without requiring CI/CD. Default True.
    :param enable_auto_session_tracking: Open one release-health session for
        the running process at init and close it on graceful shutdown with the
        final crash-free status. Default True; set False to opt out.
    :param offline_storage: Persist undeliverable telemetry (network down /
        retries exhausted / buffered at shutdown) PII-scrubbed to a filesystem
        spool and replay it on the next init. Default True; fail-open. Set
        False to disable.
    :param offline_queue_dir: Override the spool directory (default: a
        per-backend dir under the system temp dir).
    :param offline_max_events: Max persisted events kept (oldest dropped).
    :param offline_max_bytes: Max total spool bytes (oldest dropped).
    :param offline_max_age_s: Max age (seconds) of a persisted event.
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
            install_excepthook=install_excepthook,
            install_threading_excepthook=install_threading_excepthook,
            before_send=before_send,
            sample_rate=sample_rate,
            traces_sample_rate=traces_sample_rate,
            auto_register_release=auto_register_release,
            enable_auto_session_tracking=enable_auto_session_tracking,
            offline_storage=offline_storage,
            offline_queue_dir=offline_queue_dir,
            offline_max_events=offline_max_events,
            offline_max_bytes=offline_max_bytes,
            offline_max_age_s=offline_max_age_s,
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

        try:
            from .integrations.requests import install_requests
            install_requests()
        except Exception as e:  # pragma: no cover — never fail init
            logger.debug("[AllStak] requests auto-install failed: %s", e)

        try:
            from .integrations.celery import install_celery
            install_celery()
        except Exception as e:  # pragma: no cover — never fail init
            logger.debug("[AllStak] celery auto-install failed: %s", e)

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
