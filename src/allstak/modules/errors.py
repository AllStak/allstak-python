"""Error capture module — POST /ingest/v1/errors."""

from __future__ import annotations

import logging
import random
import sys
import traceback
from typing import Any, Dict, List, Optional

from ..config import AllStakConfig
from ..models.breadcrumb import Breadcrumb
from ..models.errors import ErrorPayload, RequestContext, UserContext
from ..sanitize import scrub
from ..transport import AllStakAuthError, AllStakTransportError, HttpTransport

logger = logging.getLogger("allstak.sdk")

_INGEST_PATH = "/ingest/v1/errors"
_DEFAULT_MAX_BREADCRUMBS = 50


class ErrorModule:
    SDK_VERSION = "0.1.2"

    """
    Captures exceptions and sends them to AllStak.

    Errors are sent immediately (no buffering — errors are urgent).
    All failures are swallowed to prevent crashing the host application.
    """

    def __init__(self, transport: HttpTransport, config: AllStakConfig) -> None:
        self._transport = transport
        self._config = config
        self._max_breadcrumbs = getattr(config, "max_breadcrumbs", _DEFAULT_MAX_BREADCRUMBS)
        self._current_user: Optional[UserContext] = None
        self._breadcrumbs: List[Breadcrumb] = []
        self._breadcrumb_lock = __import__("threading").Lock()

    def set_user(self, user: UserContext) -> None:
        """Set a default user context that will be attached to all subsequent errors."""
        self._current_user = user

    def clear_user(self) -> None:
        """Clear the current user context."""
        self._current_user = None

    def add_breadcrumb(
        self,
        type: str,
        message: str,
        level: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Add a breadcrumb. Oldest are dropped when the buffer exceeds 50."""
        crumb = Breadcrumb(type=type, message=message, level=level or "info", data=data)
        with self._breadcrumb_lock:
            if len(self._breadcrumbs) >= self._max_breadcrumbs:
                self._breadcrumbs.pop(0)
            self._breadcrumbs.append(crumb)

    def clear_breadcrumbs(self) -> None:
        """Clear all breadcrumbs from the buffer."""
        with self._breadcrumb_lock:
            self._breadcrumbs.clear()

    def _drain_breadcrumbs(self) -> Optional[List[Dict[str, Any]]]:
        """Drain breadcrumbs and return them as a list of dicts, or None if empty."""
        with self._breadcrumb_lock:
            if not self._breadcrumbs:
                return None
            result = [b.to_dict() for b in self._breadcrumbs]
            self._breadcrumbs.clear()
            return result

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
        trace_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        mechanism: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Capture a Python exception and send it to AllStak.

        Returns the event ID string on success, or None on failure.
        Never raises.

        :param exc: The exception to capture.
        :param level: Severity level (default: "error").
        :param environment: Override environment from init config.
        :param release: Override release from init config.
        :param session_id: Link to a session replay session.
        :param user: User context (overrides set_user()).
        :param metadata: Arbitrary key-value metadata dict.
        :param mechanism: Optional capture mechanism, e.g.
                          ``{"type": "excepthook", "handled": False}``.
        """
        try:
            frames = self._extract_stack_trace(exc)
            structured = self._extract_structured_frames(exc)
            breadcrumbs = self._drain_breadcrumbs()
            payload = ErrorPayload(
                exception_class=type(exc).__name__,
                message=str(exc) or repr(exc),
                stack_trace=frames,
                level=level,
                environment=environment or self._config.environment,
                release=release or self._config.release,
                session_id=session_id,
                user=user or self._current_user,
                request_context=request_context,
                trace_id=trace_id,
                metadata=metadata or {},
                breadcrumbs=breadcrumbs,
                # Phase 2 — v2 ingest contract
                sdk_name=getattr(self._config, "sdk_name", None) or "allstak-python",
                sdk_version=getattr(self._config, "sdk_version", None) or self.SDK_VERSION,
                platform=getattr(self._config, "platform", None) or "python",
                dist=getattr(self._config, "dist", None),
                frames=structured if structured else None,
                mechanism=mechanism,
            )
            return self._send(payload)
        except AllStakAuthError:
            raise  # let the caller (AllStakClient) handle auth disable
        except Exception as send_err:
            logger.debug("[AllStak] capture_exception failed silently: %s", send_err)
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
        request_context: Optional[RequestContext] = None,
        trace_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        mechanism: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Capture an error by class name and message (without a Python exception object).
        Useful when re-surfacing errors from external systems.

        Returns the event ID string on success, or None on failure.
        Never raises.
        """
        try:
            breadcrumbs = self._drain_breadcrumbs()
            payload = ErrorPayload(
                exception_class=exception_class,
                message=message,
                stack_trace=stack_trace or [],
                level=level,
                environment=environment or self._config.environment,
                release=release or self._config.release,
                session_id=session_id,
                user=user or self._current_user,
                request_context=request_context,
                trace_id=trace_id,
                metadata=metadata or {},
                breadcrumbs=breadcrumbs,
                mechanism=mechanism,
            )
            return self._send(payload)
        except AllStakAuthError:
            raise
        except Exception as send_err:
            logger.debug("[AllStak] capture_error failed silently: %s", send_err)
            return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _send(self, payload: ErrorPayload) -> Optional[str]:
        # Single capture chokepoint. Order:
        #   1. sample_rate drop (before before_send — dropped events never
        #      reach the user callback)
        #   2. before_send hook (may modify the event or drop it via None;
        #      fail-open if it raises)
        #   3. PII sanitize
        #   4. transport
        #
        # 1. Probabilistic sampling for error/message events.
        sample_rate = getattr(self._config, "sample_rate", 1.0)
        if sample_rate < 1.0 and random.random() >= sample_rate:
            logger.debug("[AllStak] event dropped by sample_rate=%.3f", sample_rate)
            return None

        event = payload.to_dict()

        # 2. before_send hook — runs on the structured event before sanitize.
        before_send = getattr(self._config, "before_send", None)
        if before_send is not None:
            try:
                result = before_send(event)
                if result is None:
                    logger.debug("[AllStak] event dropped by before_send")
                    return None
                event = result
            except Exception as cb_err:
                # Fail open: a user callback must never crash capture. Fall
                # back to sending the original, un-modified event.
                logger.debug("[AllStak] before_send raised (fail-open): %s", cb_err)

        # 3. Sanitize the entire wire payload before transport — covers user,
        # metadata, breadcrumbs, request_context, contexts, and any nested
        # values that match the canonical denylist. Pure: no caller mutation.
        wire_payload = scrub(event)
        status, body = self._transport.post(_INGEST_PATH, wire_payload)
        if status == 202:
            event_id: Optional[str] = None
            data = body.get("data") or {}
            if isinstance(data, dict):
                event_id = data.get("id")
            return event_id
        logger.debug("[AllStak] Error ingestion returned %d: %s", status, body)
        return None

    @staticmethod
    def _extract_stack_trace(exc: BaseException) -> List[str]:
        """Format the traceback of *exc* as a list of strings."""
        try:
            tb = exc.__traceback__
            if tb is None:
                return []
            lines = traceback.format_tb(tb)
            frames: List[str] = []
            for chunk in lines:
                for line in chunk.splitlines():
                    stripped = line.strip()
                    if stripped:
                        frames.append(stripped)
            return frames
        except Exception:
            return []

    @staticmethod
    def _extract_structured_frames(exc: BaseException) -> List[Dict[str, Any]]:
        """
        Phase 2 — produce v2-shape ``ErrorIngestRequest.Frame`` dicts from
        the traceback so the backend can resolve / display source-mapped
        frames without re-parsing the v1 string list.
        """
        out: List[Dict[str, Any]] = []
        try:
            tb = exc.__traceback__
            if tb is None:
                return out
            for f in traceback.extract_tb(tb):
                in_app = not (f.filename.startswith("<") or "site-packages" in f.filename)
                out.append({
                    "filename": f.filename,
                    "absPath": f.filename,
                    "function": f.name,
                    "lineno": f.lineno,
                    "colno": None,
                    "inApp": in_app,
                    "platform": "python",
                })
        except Exception:
            pass
        return out
