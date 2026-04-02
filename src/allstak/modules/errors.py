"""Error capture module — POST /ingest/v1/errors."""

from __future__ import annotations

import logging
import sys
import traceback
from typing import Any, Dict, List, Optional

from ..config import AllStakConfig
from ..models.errors import ErrorPayload, UserContext
from ..transport import AllStakAuthError, AllStakTransportError, HttpTransport

logger = logging.getLogger("allstak.sdk")

_INGEST_PATH = "/ingest/v1/errors"


class ErrorModule:
    """
    Captures exceptions and sends them to AllStak.

    Errors are sent immediately (no buffering — errors are urgent).
    All failures are swallowed to prevent crashing the host application.
    """

    def __init__(self, transport: HttpTransport, config: AllStakConfig) -> None:
        self._transport = transport
        self._config = config
        self._current_user: Optional[UserContext] = None

    def set_user(self, user: UserContext) -> None:
        """Set a default user context that will be attached to all subsequent errors."""
        self._current_user = user

    def clear_user(self) -> None:
        """Clear the current user context."""
        self._current_user = None

    def capture_exception(
        self,
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

        Returns the event ID string on success, or None on failure.
        Never raises.

        :param exc: The exception to capture.
        :param level: Severity level (default: "error").
        :param environment: Override environment from init config.
        :param release: Override release from init config.
        :param session_id: Link to a session replay session.
        :param user: User context (overrides set_user()).
        :param metadata: Arbitrary key-value metadata dict.
        """
        try:
            frames = self._extract_stack_trace(exc)
            payload = ErrorPayload(
                exception_class=type(exc).__name__,
                message=str(exc) or repr(exc),
                stack_trace=frames,
                level=level,
                environment=environment or self._config.environment,
                release=release or self._config.release,
                session_id=session_id,
                user=user or self._current_user,
                metadata=metadata or {},
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
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Capture an error by class name and message (without a Python exception object).
        Useful when re-surfacing errors from external systems.

        Returns the event ID string on success, or None on failure.
        Never raises.
        """
        try:
            payload = ErrorPayload(
                exception_class=exception_class,
                message=message,
                stack_trace=stack_trace or [],
                level=level,
                environment=environment or self._config.environment,
                release=release or self._config.release,
                session_id=session_id,
                user=user or self._current_user,
                metadata=metadata or {},
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
        status, body = self._transport.post(_INGEST_PATH, payload.to_dict())
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
            # format_tb returns multi-line strings; split and strip
            frames: List[str] = []
            for chunk in lines:
                for line in chunk.splitlines():
                    stripped = line.strip()
                    if stripped:
                        frames.append(stripped)
            return frames
        except Exception:
            return []
