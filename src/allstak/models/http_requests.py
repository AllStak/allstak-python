"""HTTP request ingest DTOs — mirrors HttpRequestIngestRequest on the backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

MAX_BATCH_SIZE = 100


@dataclass
class HttpRequestItem:
    """
    A single HTTP request/response record.

    Required fields: ``trace_id``, ``direction``, ``method``,
    ``host``, ``path``, ``status_code``, ``duration_ms``,
    ``request_size``, ``response_size``, ``timestamp``.
    """

    trace_id: str
    """Unique trace/request identifier."""

    direction: str
    """``"inbound"`` (request received by your app) or
    ``"outbound"`` (request made by your app)."""

    method: str
    """HTTP verb: GET, POST, PUT, DELETE, PATCH, etc."""

    host: str
    """Target hostname only (no protocol), e.g. ``"api.stripe.com"``."""

    path: str
    """URL path — strip query parameters before sending."""

    status_code: int
    """HTTP response status code."""

    duration_ms: int
    """Total round-trip duration in milliseconds (must be >= 0)."""

    request_size: int
    """Request body size in bytes."""

    response_size: int
    """Response body size in bytes."""

    timestamp: str
    """ISO-8601 UTC timestamp of when the request started,
    e.g. ``"2026-03-31T12:00:00.000Z"``."""

    request_id: Optional[str] = None
    """Stable request id used to link request telemetry to logs, spans, and errors."""

    span_id: Optional[str] = None
    """Span id for the root HTTP span when available."""

    parent_span_id: Optional[str] = None
    """Parent span id from upstream propagation when available."""

    user_id: Optional[str] = None
    """Authenticated user ID (optional)."""

    error_fingerprint: Optional[str] = None
    """Error group fingerprint if the request resulted in an error (optional)."""

    # Release-tracking metadata. Backend has first-class columns for
    # release / environment, and stores the rest inside metadata.
    release: Optional[str] = None
    environment: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        if self.direction not in ("inbound", "outbound"):
            raise ValueError(
                f"Invalid direction '{self.direction}'. Must be 'inbound' or 'outbound'."
            )
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be >= 0")
        payload: Dict[str, Any] = {
            "traceId": self.trace_id,
            "direction": self.direction,
            "method": self.method.upper(),
            "host": self.host,
            "path": self.path,
            "statusCode": self.status_code,
            "durationMs": self.duration_ms,
            "requestSize": self.request_size,
            "responseSize": self.response_size,
            "timestamp": self.timestamp,
        }
        if self.request_id is not None:
            payload["requestId"] = self.request_id
        if self.span_id is not None:
            payload["spanId"] = self.span_id
        if self.parent_span_id is not None:
            payload["parentSpanId"] = self.parent_span_id
        if self.user_id is not None:
            payload["userId"] = self.user_id
        if self.error_fingerprint is not None:
            payload["errorFingerprint"] = self.error_fingerprint
        if self.release is not None:
            payload["release"] = self.release
        if self.environment is not None:
            payload["environment"] = self.environment
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


@dataclass
class HttpRequestBatch:
    """
    Batch payload for ``POST /ingest/v1/http-requests``.
    Max 100 items per batch.
    """

    requests: List[HttpRequestItem] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        if not self.requests:
            raise ValueError("requests batch must contain at least 1 item")
        if len(self.requests) > MAX_BATCH_SIZE:
            raise ValueError(
                f"requests batch exceeds max size of {MAX_BATCH_SIZE} items "
                f"(got {len(self.requests)})"
            )
        return {"requests": [r.to_dict() for r in self.requests]}
