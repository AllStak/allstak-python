"""Log ingest DTO — mirrors LogIngestRequest on the backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# Valid level values for log ingestion (backend pattern: debug|info|warn|error|fatal)
# Note: "warning" is NOT valid for logs — use "warn"
LOG_LEVELS = frozenset({"debug", "info", "warn", "error", "fatal"})


@dataclass
class LogPayload:
    """
    Payload for ``POST /ingest/v1/logs``.

    Required fields: ``level``, ``message``.
    """

    level: str
    """Log severity.  One of: debug, info, warn, error, fatal.
    Note: "warning" is NOT accepted — use "warn"."""

    message: str
    """Log message text."""

    service: Optional[str] = None
    """Service or module name."""

    trace_id: Optional[str] = None
    """Distributed trace correlation ID."""

    environment: Optional[str] = None
    """Deployment environment."""

    span_id: Optional[str] = None
    """Span ID for distributed tracing."""

    request_id: Optional[str] = None
    """HTTP request correlation ID."""

    user_id: Optional[str] = None
    """Current user ID."""

    error_id: Optional[str] = None
    """Link to error if log relates to one."""

    metadata: Dict[str, Any] = field(default_factory=dict)
    """Arbitrary key-value context."""

    def to_dict(self) -> Dict[str, Any]:
        if self.level not in LOG_LEVELS:
            raise ValueError(
                f"Invalid log level '{self.level}'. "
                f"Must be one of: {sorted(LOG_LEVELS)}. "
                f"Note: use 'warn' not 'warning'."
            )
        payload: Dict[str, Any] = {
            "level": self.level,
            "message": self.message,
        }
        if self.service:
            payload["service"] = self.service
        if self.trace_id:
            payload["traceId"] = self.trace_id
        if self.environment:
            payload["environment"] = self.environment
        if self.span_id:
            payload["spanId"] = self.span_id
        if self.request_id:
            payload["requestId"] = self.request_id
        if self.user_id:
            payload["userId"] = self.user_id
        if self.error_id:
            payload["errorId"] = self.error_id
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload
