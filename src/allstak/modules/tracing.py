"""Distributed tracing module -- POST /ingest/v1/spans."""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from contextvars import ContextVar
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
        sampled: bool = True,
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
        self._sampled = sampled

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

    @property
    def sampled(self) -> bool:
        """Whether this span's trace was sampled (and will be sent)."""
        return self._sampled

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
        self._current_trace_id: ContextVar[Optional[str]] = ContextVar(
            "allstak_trace_id", default=None
        )
        self._span_stack: ContextVar[List[str]] = ContextVar(
            "allstak_span_stack", default=[]
        )
        # Per-trace sampling decision. None = "not yet decided for this trace".
        # When traces_sample_rate is None the SDK is always-on (backward compat).
        self._sampled: ContextVar[Optional[bool]] = ContextVar(
            "allstak_trace_sampled", default=None
        )
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
        trace_id = self._current_trace_id.get()
        if trace_id is None:
            trace_id = uuid.uuid4().hex
            self._current_trace_id.set(trace_id)
        return trace_id

    def set_trace_id(self, trace_id: str) -> None:
        """Set the trace ID explicitly (e.g. from an incoming request header)."""
        self._current_trace_id.set(trace_id)
        # A fresh trace context — re-decide sampling on next access.
        self._sampled.set(None)

    def is_sampled(self) -> bool:
        """Return the sampling decision for the current trace.

        When ``traces_sample_rate`` is ``None`` (default), tracing is always-on
        and this returns ``True`` (backward compatible). When a rate is set, the
        decision is made once per trace (``random.random() < rate``) and cached
        so every span and the propagated ``traceparent`` agree.
        """
        rate = getattr(self._config, "traces_sample_rate", None)
        if rate is None:
            return True
        decided = self._sampled.get()
        if decided is None:
            decided = random.random() < rate
            self._sampled.set(decided)
        return decided

    def get_current_span_id(self) -> Optional[str]:
        """Get the current active span ID (top of the stack), or None."""
        stack = self._span_stack.get()
        return stack[-1] if stack else None

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
        stack = list(self._span_stack.get())
        parent_span_id = stack[-1] if stack else ""
        trace_id = self.get_trace_id()
        # Decide (or reuse) the per-trace sampling decision so all spans in a
        # trace and the propagated traceparent agree.
        sampled = self.is_sampled()
        stack.append(span_id)
        self._span_stack.set(stack)

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
            sampled=sampled,
        )
        return span

    def reset_trace(self) -> None:
        """Clear the current trace ID and span stack."""
        self._current_trace_id.set(None)
        self._span_stack.set([])
        self._sampled.set(None)

    def flush(self) -> None:
        """Explicitly flush all completed spans."""
        self._flush_buffer.flush()

    def shutdown(self) -> None:
        """Drain the buffer and stop the background timer."""
        self._flush_buffer.stop()

    # -- Internal --

    def _on_span_finish(self, span: Span) -> None:
        """Called when a span finishes. Removes it from the stack and buffers it."""
        stack = list(self._span_stack.get())
        try:
            stack.remove(span.span_id)
        except ValueError:
            pass
        self._span_stack.set(stack)
        # Drop spans whose trace was not sampled — keep the stack consistent
        # (popped above) but never buffer/send them.
        if not span.sampled:
            return
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
