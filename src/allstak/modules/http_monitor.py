"""HTTP request monitoring module — POST /ingest/v1/http-requests."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Generator, List, Optional
from urllib.parse import urlparse

from ..buffer import FlushBuffer
from ..config import AllStakConfig
from ..models.http_requests import MAX_BATCH_SIZE, HttpRequestBatch, HttpRequestItem
from ..transport import AllStakAuthError, AllStakTransportError, HttpTransport

logger = logging.getLogger("allstak.sdk")

_INGEST_PATH = "/ingest/v1/http-requests"
_FLUSH_THRESHOLD = 50  # flush early when buffer has this many items

# Headers that must never be forwarded in captured metadata
_SENSITIVE_HEADERS = frozenset({
    "authorization", "cookie", "x-allstak-key", "x-api-key",
    "x-auth-token", "x-access-token",
})

# Query param names that must be stripped from paths
_SENSITIVE_PARAM_PATTERNS = frozenset({
    "token", "key", "secret", "password", "auth", "api_key",
    "access_token", "refresh_token",
})


def _strip_sensitive_params(path_with_query: str) -> str:
    """Remove query string — never log raw query strings."""
    parsed = urlparse(path_with_query)
    return parsed.path or path_with_query


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _OutboundRecorder:
    """
    Mutable value holder returned by :meth:`HttpMonitorModule.track_outbound`.

    Callers use ``set_response(status, size)`` to attach the real HTTP
    response status and body size before the context manager exits.
    """

    __slots__ = ("status_code", "response_size", "request_size")

    def __init__(self) -> None:
        self.status_code: int = 0
        self.response_size: int = 0
        self.request_size: int = 0

    def set_response(self, status_code: int, response_size: int = 0) -> None:
        self.status_code = int(status_code)
        self.response_size = int(response_size)

    def set_request_size(self, request_size: int) -> None:
        self.request_size = int(request_size)


class HttpMonitorModule:
    """
    Buffers and batches HTTP request telemetry for delivery to AllStak.

    Batches are flushed every ``flush_interval_ms`` ms or when
    ``_FLUSH_THRESHOLD`` items accumulate.

    Max batch size: 100 (backend enforced).
    """

    def __init__(self, transport: HttpTransport, config: AllStakConfig) -> None:
        self._transport = transport
        self._config = config
        self._flush_buffer: FlushBuffer[HttpRequestItem] = FlushBuffer(
            flush_fn=self._flush_batch,
            maxsize=config.buffer_size,
            interval_ms=config.flush_interval_ms,
            name="allstak-http-flush",
        )
        self._flush_buffer.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        direction: str,
        method: str,
        host: str,
        path: str,
        status_code: int,
        duration_ms: int,
        request_size: int = 0,
        response_size: int = 0,
        trace_id: Optional[str] = None,
        user_id: Optional[str] = None,
        error_fingerprint: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> None:
        """
        Record a single HTTP request/response.

        :param direction: ``"inbound"`` or ``"outbound"``
        :param method: HTTP verb (GET, POST, etc.)
        :param host: Target hostname only (no protocol)
        :param path: URL path only — query params are stripped automatically
        :param status_code: HTTP response status code
        :param duration_ms: Round-trip duration in milliseconds (>= 0)
        :param request_size: Request body size in bytes
        :param response_size: Response body size in bytes
        :param trace_id: Trace correlation ID (auto-generated UUID if omitted)
        :param user_id: Authenticated user ID
        :param error_fingerprint: Error group fingerprint if request failed
        :param timestamp: ISO-8601 UTC start time (defaults to now)
        """
        try:
            item = HttpRequestItem(
                trace_id=trace_id or str(uuid.uuid4()),
                direction=direction,
                method=method,
                host=host,
                path=_strip_sensitive_params(path),
                status_code=status_code,
                duration_ms=max(0, duration_ms),
                request_size=request_size,
                response_size=response_size,
                timestamp=timestamp or _now_iso(),
                user_id=user_id,
                error_fingerprint=error_fingerprint,
            )
            # Validate
            item.to_dict()
            self._flush_buffer.push(item)
        except Exception as exc:
            logger.debug("[AllStak] http_monitor.record() failed silently: %s", exc)

    @contextmanager
    def track_outbound(
        self,
        method: str,
        url: str,
        *,
        user_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> Generator["_OutboundRecorder", None, None]:
        """
        Context manager that times an outbound HTTP call and records it.

        Yields an :class:`_OutboundRecorder` so the caller can attach the real
        response status and size::

            with allstak.http.track_outbound("GET", "https://api.example.com/data") as call:
                resp = httpx.get("https://api.example.com/data")
                call.set_response(resp.status_code, len(resp.content))

        On an unhandled exception the call is still recorded (status=0,
        error fingerprint = exception class name) and the exception re-raised.
        """
        parsed = urlparse(url)
        host = parsed.netloc or parsed.path
        path = parsed.path or "/"
        start_ms = time.monotonic() * 1000
        start_ts = _now_iso()
        recorder = _OutboundRecorder()
        exc_type_name: Optional[str] = None
        try:
            yield recorder
        except Exception as exc:
            exc_type_name = type(exc).__name__
            raise
        finally:
            duration_ms = int(time.monotonic() * 1000 - start_ms)
            self.record(
                direction="outbound",
                method=method,
                host=host,
                path=path,
                status_code=recorder.status_code or 0,
                duration_ms=duration_ms,
                request_size=recorder.request_size,
                response_size=recorder.response_size,
                timestamp=start_ts,
                user_id=user_id,
                trace_id=trace_id,
                error_fingerprint=exc_type_name,
            )

    def flush(self) -> None:
        """Explicitly flush all buffered HTTP requests."""
        self._flush_buffer.flush()

    def shutdown(self) -> None:
        """Drain the buffer and stop the timer thread."""
        self._flush_buffer.stop()

    # ------------------------------------------------------------------
    # Internal flush
    # ------------------------------------------------------------------

    def _flush_batch(self, items: List[HttpRequestItem]) -> None:
        """Send items in batches of up to MAX_BATCH_SIZE."""
        # Chunk into batches of 100
        for i in range(0, len(items), MAX_BATCH_SIZE):
            chunk = items[i : i + MAX_BATCH_SIZE]
            try:
                batch = HttpRequestBatch(requests=chunk)
                status, body = self._transport.post(
                    _INGEST_PATH, batch.to_dict()
                )
                if status != 202:
                    logger.debug(
                        "[AllStak] HTTP requests batch returned %d: %s", status, body
                    )
            except AllStakAuthError:
                logger.warning("[AllStak] HTTP batch skipped — SDK disabled (invalid API key).")
                return
            except AllStakTransportError as exc:
                logger.debug("[AllStak] HTTP batch transport error (discarding): %s", exc)
            except Exception as exc:
                logger.debug("[AllStak] Unexpected HTTP batch error: %s", exc)
