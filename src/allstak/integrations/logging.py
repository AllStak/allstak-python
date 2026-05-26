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

            if record.levelno >= self.event_level:
                self._capture_event(client, record)
            elif record.levelno >= self.breadcrumb_level:
                self._add_breadcrumb(client, record)
        except Exception:
            # A logging handler must never raise out of emit().
            pass

    def _add_breadcrumb(self, client: object, record: logging.LogRecord) -> None:
        client.add_breadcrumb(  # type: ignore[attr-defined]
            type="log",
            message=record.getMessage(),
            level=_breadcrumb_level_name(record.levelno),
            data={"logger": record.name, "module": record.module},
        )
        # Tell the auto-breadcrumb handler this record is already handled.
        setattr(record, _ALLSTAK_LOG_HANDLED, True)

    def _capture_event(self, client: object, record: logging.LogRecord) -> None:
        metadata = {
            "logger": record.name,
            "module": record.module,
            "level": record.levelname,
        }
        exc = None
        if record.exc_info and isinstance(record.exc_info, tuple):
            exc = record.exc_info[1]

        if exc is not None:
            client.capture_exception(  # type: ignore[attr-defined]
                exc,
                level="error" if record.levelno >= logging.ERROR else "warn",
                metadata=metadata,
                mechanism={"type": "logging", "handled": True},
            )
        else:
            client.capture_error(  # type: ignore[attr-defined]
                record.name,
                record.getMessage(),
                level="error" if record.levelno >= logging.ERROR else "warn",
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
