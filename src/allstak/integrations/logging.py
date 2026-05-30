"""
``logging`` integration for AllStak — forwards standard-library log records to
AllStak as **error events** (not just breadcrumbs).

This is distinct from the WARNING+ breadcrumb handler in
:mod:`allstak.integrations.auto_breadcrumbs`:

* ``level`` (default ``ERROR``): records at/above this level become **captured
  error events**, including the exception + stack when the record carries
  ``exc_info`` (e.g. ``logger.exception(...)`` or ``logger.error(..., exc_info=True)``).
* ``breadcrumb_level`` (default ``INFO``): records at/above this level but below
  ``level`` become **breadcrumbs** for context on the next error.

To avoid DOUBLE breadcrumbs when the auto-breadcrumb handler is also active,
each record this handler turns into a breadcrumb is stamped with
``_ALLSTAK_LOG_HANDLED``; the auto-breadcrumb handler skips stamped records.

Install once at startup::

    import logging
    from allstak.integrations.logging import install_logging

    install_logging(level=logging.ERROR, breadcrumb_level=logging.INFO)

Idempotent — a second call replaces the existing handler rather than stacking.
Degrades gracefully: ``logging`` is part of the stdlib, so this never hard-fails.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("allstak.sdk")

# Stamped on a LogRecord once this handler has turned it into a breadcrumb, so
# the auto-breadcrumb handler in ``auto_breadcrumbs.py`` does not also add one.
_ALLSTAK_LOG_HANDLED = "_allstak_log_breadcrumbed"

# Sentinel attribute on a logger marking that our handler is attached, used to
# locate + replace it on a re-install (idempotency).
_HANDLER_MARKER = "_allstak_event_handler"

# Attribute stamped on an *exception instance* by a framework integration once
# it has already reported that exception (e.g. Django's got_request_exception
# receiver or the FastAPI/ASGI middleware). The logging bridge skips a record
# whose ``exc_info`` exception carries this marker so the framework logger's
# follow-up ``ERROR ... exc_info`` line does not re-report the same exception.
_EXC_CAPTURED_MARKER = "_allstak_captured"


def mark_exception_captured(exc: BaseException) -> None:
    """Stamp ``exc`` so the logging bridge will not re-report it.

    Called by framework integrations right after they capture an exception, so
    the framework's own follow-up ``logger.error(..., exc_info=...)`` (e.g.
    ``django.request``'s "Internal Server Error") is not turned into a second
    error event. Best-effort: some exception types disallow attribute writes,
    in which case dedup falls back to the integration's own per-request latch.
    """
    try:
        setattr(exc, _EXC_CAPTURED_MARKER, True)
    except Exception:
        pass


def _exception_already_captured(exc: Optional[BaseException]) -> bool:
    """Whether ``exc`` was already reported by a framework integration."""
    if exc is None:
        return False
    try:
        return bool(getattr(exc, _EXC_CAPTURED_MARKER, False))
    except Exception:
        return False


def _is_framework_control_flow(exc: Optional[BaseException]) -> bool:
    """Whether ``exc`` is a framework control-flow exception, not an app error.

    Web frameworks raise/log certain exceptions to drive routing or auth and map
    them to 4xx responses — they are expected control flow, not application
    errors, and the request integrations deliberately keep them out of the error
    stream. The logging bridge must honour the same boundary so the framework's
    own ``logger.error(..., exc_info=...)`` for one of these (e.g. Django logging
    a ``SuspiciousOperation``) is not promoted to an error event.

    Each framework is probed lazily and the probe is a no-op when that framework
    is not installed, so this never hard-depends on Django/Starlette. Never
    raises.
    """
    if exc is None:
        return False
    # Django: Http404 / PermissionDenied / SuspiciousOperation -> 4xx responses.
    try:
        from django.core.exceptions import (  # type: ignore[import-untyped]
            PermissionDenied,
            SuspiciousOperation,
        )
        from django.http import Http404  # type: ignore[import-untyped]

        if isinstance(exc, (Http404, PermissionDenied, SuspiciousOperation)):
            return True
    except Exception:
        pass
    return False

_LEVEL_TO_BREADCRUMB = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}


def _breadcrumb_level_name(levelno: int) -> str:
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warn"
    if levelno >= logging.INFO:
        return "info"
    return "debug"


# LogRecord attributes commonly used by request-scoped logging filters to stamp
# a per-request correlation id (e.g. a Django/Flask middleware or a
# ``logging.Filter`` injecting ``record.request_id``). The first present,
# non-empty one wins.
_REQUEST_ID_ATTRS = ("requestId", "request_id", "request")


def _request_id_from_record(record: logging.LogRecord) -> Optional[str]:
    """Pull a request-correlation id off a LogRecord, if a filter set one.

    Returns the first non-empty value among the common attribute names, or
    ``None``. Never raises.
    """
    for attr in _REQUEST_ID_ATTRS:
        try:
            value = getattr(record, attr, None)
        except Exception:
            value = None
        if value:
            text = str(value)
            if text:
                return text
    return None


class AllStakLoggingHandler(logging.Handler):
    """
    A ``logging.Handler`` that forwards records to AllStak.

    Records at/above ``level`` are captured as error events (with the
    exception + stack when ``exc_info`` is present). Records at/above
    ``breadcrumb_level`` (but below ``level``) are added as breadcrumbs.
    """

    def __init__(self, level: int = logging.ERROR, breadcrumb_level: int = logging.INFO) -> None:
        # The handler must SEE everything from breadcrumb_level up so it can
        # decide between breadcrumb and event; ``self.event_level`` is the
        # event threshold.
        super().__init__(level=min(level, breadcrumb_level))
        self.event_level = level
        self.breadcrumb_level = breadcrumb_level

    def emit(self, record: logging.LogRecord) -> None:
        # Never recurse on our own SDK logs.
        if record.name.startswith("allstak"):
            return
        try:
            import allstak

            client = allstak.get_client()
            if client is None:
                return

            # Skip records whose exception a framework integration already
            # reported (e.g. ``django.request`` logging the same unhandled
            # exception the got_request_exception receiver captured). Prevents
            # a double error event without suppressing genuine app logs.
            exc = (
                record.exc_info[1]
                if record.exc_info and isinstance(record.exc_info, tuple)
                else None
            )
            if _exception_already_captured(exc):
                setattr(record, _ALLSTAK_LOG_HANDLED, True)
                return

            # Framework control-flow exceptions (Django Http404 /
            # PermissionDenied / SuspiciousOperation) are 4xx, not app errors.
            # The request integrations keep them out of the error stream, so the
            # framework's follow-up error log must not promote them either — it
            # degrades to a breadcrumb for context instead.
            if record.levelno >= self.event_level and _is_framework_control_flow(exc):
                if record.levelno >= self.breadcrumb_level:
                    self._add_breadcrumb(client, record)
                else:
                    setattr(record, _ALLSTAK_LOG_HANDLED, True)
                return

            if record.levelno >= self.event_level:
                self._capture_event(client, record)
            elif record.levelno >= self.breadcrumb_level:
                self._add_breadcrumb(client, record)
        except Exception:
            # A logging handler must never raise out of emit().
            pass

    def _add_breadcrumb(self, client: object, record: logging.LogRecord) -> None:
        data = {"logger": record.name, "module": record.module}
        request_id = _request_id_from_record(record)
        if request_id:
            data["requestId"] = request_id
        client.add_breadcrumb(  # type: ignore[attr-defined]
            type="log",
            message=record.getMessage(),
            level=_breadcrumb_level_name(record.levelno),
            data=data,
        )
        # Tell the auto-breadcrumb handler this record is already handled.
        setattr(record, _ALLSTAK_LOG_HANDLED, True)

    def _capture_event(self, client: object, record: logging.LogRecord) -> None:
        metadata = {
            "logger": record.name,
            "module": record.module,
            "level": record.levelname,
        }
        # Stamp trace / request correlation ids so a forwarded log lines up with
        # the request and trace that produced it. ``traceId`` / ``spanId`` come
        # from the active SDK trace context (client merges them in too, but we
        # set them here so they survive even if the active trace shifts before
        # send); ``requestId`` comes off the LogRecord when a request-scoped
        # logging filter put it there (common patterns: ``record.request_id`` /
        # ``record.requestId`` / ``record.request``).
        try:
            trace_id = client.get_trace_id()  # type: ignore[attr-defined]
            if trace_id:
                metadata["traceId"] = trace_id
        except Exception:
            pass
        try:
            span_id = client.get_current_span_id()  # type: ignore[attr-defined]
            if span_id:
                metadata["spanId"] = span_id
        except Exception:
            pass
        request_id = _request_id_from_record(record)
        if request_id:
            metadata["requestId"] = request_id

        exc = None
        if record.exc_info and isinstance(record.exc_info, tuple):
            exc = record.exc_info[1]

        # Promote ERROR/FATAL records to the errors stream. ``logger.exception``
        # / ``exc_info`` records carry the exception + stack and go through
        # ``capture_exception``; plain messages go through ``capture_error``.
        level = "fatal" if record.levelno >= logging.CRITICAL else "error"
        if record.levelno < logging.ERROR:
            level = "warn"

        if exc is not None:
            client.capture_exception(  # type: ignore[attr-defined]
                exc,
                level=level,
                metadata=metadata,
                mechanism={"type": "logging", "handled": True},
            )
        else:
            client.capture_error(  # type: ignore[attr-defined]
                record.name,
                record.getMessage(),
                level=level,
                metadata=metadata,
            )
        # An error event already implies a breadcrumb's worth of context, and we
        # don't want the auto-breadcrumb handler to also crumb it.
        setattr(record, _ALLSTAK_LOG_HANDLED, True)


def install_logging(
    level: int = logging.ERROR,
    breadcrumb_level: int = logging.INFO,
    logger_name: Optional[str] = None,
) -> AllStakLoggingHandler:
    """
    Attach :class:`AllStakLoggingHandler` to the root logger (or ``logger_name``).

    * ``level`` — records at/above this level become captured error events
      (default ``logging.ERROR``).
    * ``breadcrumb_level`` — records at/above this level (but below ``level``)
      become breadcrumbs (default ``logging.INFO``).

    Idempotent: a second call removes the previously installed handler before
    attaching a fresh one (so changing levels works, without stacking). Returns
    the installed handler.
    """
    target = logging.getLogger(logger_name) if logger_name else logging.getLogger()

    # Idempotency: drop any handler we previously installed on this logger.
    for existing in list(target.handlers):
        if getattr(existing, _HANDLER_MARKER, False):
            target.removeHandler(existing)

    handler = AllStakLoggingHandler(level=level, breadcrumb_level=breadcrumb_level)
    setattr(handler, _HANDLER_MARKER, True)
    # Insert FIRST so this handler stamps the record (``_ALLSTAK_LOG_HANDLED``)
    # before the auto-breadcrumb handler sees it — handlers fire in list order,
    # and stamping first is what prevents DOUBLE breadcrumbs.
    target.addHandler(handler)
    target.handlers.remove(handler)
    target.handlers.insert(0, handler)
    # Ensure the root logger actually lets records through to handlers.
    if target.level == logging.NOTSET or target.level > min(level, breadcrumb_level):
        target.setLevel(min(level, breadcrumb_level))

    logger.debug("AllStak logging handler installed (level=%s, breadcrumb_level=%s)", level, breadcrumb_level)
    return handler
