"""Log ingestion module — POST /ingest/v1/logs."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..buffer import FlushBuffer
from ..config import AllStakConfig
from ..models.logs import LOG_LEVELS, LogPayload
from ..transport import AllStakAuthError, AllStakTransportError, HttpTransport

logger = logging.getLogger("allstak.sdk")

_INGEST_PATH = "/ingest/v1/logs"


_BREADCRUMB_LOG_LEVELS = frozenset({"warn", "error", "fatal"})


class LogModule:
    """
    Buffers log events and flushes them to AllStak in the background.

    Logs do NOT have a batch endpoint — each log is sent as a separate
    HTTP request.  The buffer absorbs high-throughput logging without
    blocking the application thread.
    """

    def __init__(self, transport: HttpTransport, config: AllStakConfig) -> None:
        self._transport = transport
        self._config = config
        self._on_log_breadcrumb: Optional[Callable[..., None]] = None
        self._flush_buffer: FlushBuffer[LogPayload] = FlushBuffer(
            flush_fn=self._flush_batch,
            maxsize=config.buffer_size,
            interval_ms=config.flush_interval_ms,
            name="allstak-logs-flush",
        )
        self._flush_buffer.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_on_log_breadcrumb(self, callback: Callable[..., None]) -> None:
        """Register a callback for auto-breadcrumbs on warn/error/fatal logs."""
        self._on_log_breadcrumb = callback

    def log(
        self,
        level: str,
        message: str,
        *,
        service: Optional[str] = None,
        trace_id: Optional[str] = None,
        environment: Optional[str] = None,
        span_id: Optional[str] = None,
        request_id: Optional[str] = None,
        user_id: Optional[str] = None,
        error_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Buffer a log entry for async delivery.

        :param level: One of: ``debug``, ``info``, ``warn``, ``error``, ``fatal``.
                      Note: use ``warn`` not ``warning``.
        :param message: Log message text.
        :param service: Optional service/module name.
        :param trace_id: Optional distributed trace ID.
        :param environment: Optional deployment environment.
        :param span_id: Optional span ID for distributed tracing.
        :param request_id: Optional HTTP request correlation ID.
        :param user_id: Optional current user ID.
        :param error_id: Optional link to error if log relates to one.
        :param metadata: Optional arbitrary key-value dict.
        """
        try:
            payload = LogPayload(
                level=level,
                message=message,
                service=service,
                trace_id=trace_id,
                environment=environment,
                span_id=span_id,
                request_id=request_id,
                user_id=user_id,
                error_id=error_id,
                metadata=metadata or {},
            )
            # Validate before buffering (raises ValueError on bad level)
            payload.to_dict()

            # Auto-breadcrumb for warn/error/fatal log levels
            if self._on_log_breadcrumb and level in _BREADCRUMB_LOG_LEVELS:
                try:
                    bc_level = "error" if level in ("error", "fatal") else "warn"
                    self._on_log_breadcrumb(
                        type="log",
                        message=message,
                        level=bc_level,
                        data={"logLevel": level, **({"service": service} if service else {})},
                    )
                except Exception:
                    pass

            self._flush_buffer.push(payload)
        except Exception as exc:
            logger.debug("[AllStak] log() failed silently: %s", exc)

    # Convenience wrappers
    def debug(self, message: str, **kwargs: Any) -> None:
        self.log("debug", message, **kwargs)

    def info(self, message: str, **kwargs: Any) -> None:
        self.log("info", message, **kwargs)

    def warn(self, message: str, **kwargs: Any) -> None:
        self.log("warn", message, **kwargs)

    # alias
    warning = warn

    def error(self, message: str, **kwargs: Any) -> None:
        self.log("error", message, **kwargs)

    def fatal(self, message: str, **kwargs: Any) -> None:
        self.log("fatal", message, **kwargs)

    def flush(self) -> None:
        """Explicitly flush all buffered logs."""
        self._flush_buffer.flush()

    def shutdown(self) -> None:
        """Drain the buffer and stop the timer thread."""
        self._flush_buffer.stop()

    # ------------------------------------------------------------------
    # Internal flush
    # ------------------------------------------------------------------

    def _flush_batch(self, items: List[LogPayload]) -> None:
        """Called by FlushBuffer — send each log as an individual request."""
        for payload in items:
            try:
                status, body = self._transport.post(_INGEST_PATH, payload.to_dict())
                if status != 202:
                    logger.debug(
                        "[AllStak] Log ingestion returned %d: %s", status, body
                    )
            except AllStakAuthError:
                logger.warning("[AllStak] Log flush skipped — SDK is disabled (invalid API key).")
                return  # abort remaining items too
            except AllStakTransportError as exc:
                logger.debug("[AllStak] Log transport error (discarding): %s", exc)
            except Exception as exc:
                logger.debug("[AllStak] Unexpected log flush error: %s", exc)
