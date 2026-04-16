"""Distributed tracing module -- POST /ingest/v1/spans."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from ..buffer import FlushBuffer
from ..config import AllStakConfig
from ..transport import AllStakAuthError, AllStakTransportError, HttpTransport

logger = logging.getLogger("allstak.sdk")

_INGEST_PATH = "/ingest/v1/spans"
_BATCH_SIZE_THRESHOLD = 20


class Span:
    """
    Represents a single span in a distributed trace.

    Can be used as a context manager::

        with tracing.start_span("db.query") as span:
            span.set_tag("db.type", "postgresql")
            result = db.execute(query)
    """

    def __init__(
        self,
        trace_id: str,
        span_id: str,
        parent_span_id: str,
        operation: str,
        description: str,
        service: str,
        environment: str,
        tags: Dict[str, str],
        start_time_millis: int,
        on_finish: Callable[["Span"], None],
    ) -> None:
        self._trace_id = trace_id
        self._span_id = span_id
        self._parent_span_id = parent_span_id
        self._operation = operation
        self._description = description
        self._service = service
        self._environment = environment
        self._tags = dict(tags)
        self._data = ""
        self._start_time_millis = start_time_millis
        self._end_time_millis: Optional[int] = None
        self._status: str = "ok"
        self._finished = False
        self._on_finish = on_finish

    # -- Public API --

    @property
    def trace_id(self) -> str:
        return self._trace_id

    @property
    def span_id(self) -> str:
        return self._span_id

    @property
    def parent_span_id(self) -> str:
        return self._parent_span_id

    @property
    def is_finished(self) -> bool:
        return self._finished

    def set_tag(self, key: str, value: str) -> "Span":
        """Set a key-value tag on this span."""
        self._tags[key] = value
        return self

    def set_data(self, data: str) -> "Span":
        """Set arbitrary string data on this span."""
        self._data = data
        return self

    def set_description(self, description: str) -> "Span":
        """Update the description after creation."""
        self._description = description
        return self

    def finish(self, status: str = "ok") -> None:
        """
        Finish the span with the given status ('ok', 'error', 'timeout').
        Calling finish() more than once is a no-op.
        """
        if self._finished:
            return
        self._finished = True
        if status not in ("ok", "error", "timeout"):
            status = "ok"
        self._status = status
        self._end_time_millis = _now_millis()
        self._on_finish(self)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize span to the ingest payload format."""
        end = self._end_time_millis or _now_millis()
        return {
            "traceId": self._trace_id,
            "spanId": self._span_id,
            "parentSpanId": self._parent_span_id,
            "operation": self._operation,
            "description": self._description,
            "status": self._status,
            "durationMs": end - self._start_time_millis,
            "startTimeMillis": self._start_time_millis,
            "endTimeMillis": end,
            "service": self._service,
            "environment": self._environment,
            "tags": self._tags,
            "data": self._data,
        }

    # -- Context manager --

    def __enter__(self) -> "Span":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if not self._finished:
            status = "error" if exc_type is not None else "ok"
            self.finish(status)


class TracingModule:
    """
    Manages distributed trace context and span lifecycle.

    Spans are batched and flushed to the backend via the same
    FlushBuffer mechanism used by other modules.
    """

    def __init__(self, transport: HttpTransport, config: AllStakConfig) -> None:
        self._transport = transport
        self._config = config
        self._service = ""
        self._environment = config.environment or ""
        self._lock = threading.RLock()
        self._current_trace_id: Optional[str] = None
        self._span_stack: List[str] = []
        self._flush_buffer: FlushBuffer[Span] = FlushBuffer(
            flush_fn=self._flush_batch,
            maxsize=config.buffer_size,
            interval_ms=config.flush_interval_ms,
            name="allstak-tracing-flush",
        )
        self._flush_buffer.start()

    # -- Public API --

    def set_service(self, service: str) -> None:
        """Set the service name attached to all spans."""
        self._service = service

    def get_trace_id(self) -> str:
        """Get the current trace ID, creating one if none exists."""
        with self._lock:
            if self._current_trace_id is None:
                self._current_trace_id = uuid.uuid4().hex
            return self._current_trace_id

    def set_trace_id(self, trace_id: str) -> None:
        """Set the trace ID explicitly (e.g. from an incoming request header)."""
        with self._lock:
            self._current_trace_id = trace_id

    def get_current_span_id(self) -> Optional[str]:
        """Get the current active span ID (top of the stack), or None."""
        with self._lock:
            return self._span_stack[-1] if self._span_stack else None

    def start_span(
        self,
        operation: str,
        *,
        description: str = "",
        tags: Optional[Dict[str, str]] = None,
    ) -> Span:
        """
        Start a new span. The span is automatically parented to the
        current active span (if any).

        Can be used as a context manager::

            with tracing.start_span("http.request", description="GET /api/users") as span:
                span.set_tag("http.status", "200")
                ...

        Or manually::

            span = tracing.start_span("db.query")
            try:
                result = db.execute(sql)
                span.finish("ok")
            except Exception:
                span.finish("error")
                raise
        """
        span_id = uuid.uuid4().hex
        with self._lock:
            parent_span_id = self._span_stack[-1] if self._span_stack else ""
            trace_id = self.get_trace_id()
            self._span_stack.append(span_id)

        span = Span(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            operation=operation,
            description=description,
            service=self._service,
            environment=self._environment,
            tags=tags or {},
            start_time_millis=_now_millis(),
            on_finish=self._on_span_finish,
        )
        return span

    def reset_trace(self) -> None:
        """Clear the current trace ID and span stack."""
        with self._lock:
            self._current_trace_id = None
            self._span_stack = []

    def flush(self) -> None:
        """Explicitly flush all completed spans."""
        self._flush_buffer.flush()

    def shutdown(self) -> None:
        """Drain the buffer and stop the background timer."""
        self._flush_buffer.stop()

    # -- Internal --

    def _on_span_finish(self, span: Span) -> None:
        """Called when a span finishes. Removes it from the stack and buffers it."""
        with self._lock:
            try:
                self._span_stack.remove(span.span_id)
            except ValueError:
                pass
        self._flush_buffer.push(span)

    def _flush_batch(self, items: List[Span]) -> None:
        """Send spans to the backend in a single batch."""
        if not items:
            return
        payload = {"spans": [s.to_dict() for s in items]}
        try:
            status, body = self._transport.post(_INGEST_PATH, payload)
            if status != 202:
                logger.debug(
                    "[AllStak] Span ingestion returned %d: %s", status, body
                )
        except AllStakAuthError:
            logger.warning(
                "[AllStak] Span flush skipped -- SDK is disabled (invalid API key)."
            )
        except AllStakTransportError as exc:
            logger.debug("[AllStak] Span transport error (discarding): %s", exc)
        except Exception as exc:
            logger.debug("[AllStak] Unexpected span flush error: %s", exc)


def _now_millis() -> int:
    """Current time in milliseconds since epoch."""
    return int(time.time() * 1000)
