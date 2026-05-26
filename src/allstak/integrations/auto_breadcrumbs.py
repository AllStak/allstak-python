"""Automatic breadcrumb instrumentation for common Python libraries."""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("allstak.sdk")

_SENSITIVE_PARAMS = {"token", "key", "secret", "password", "auth", "api_key"}

# Sentinel attribute stamped on our patched ``Session.send`` so re-init (or both
# instrumentation paths running) does not double-stack the breadcrumb wrapper.
_BREADCRUMB_PATCH_MARKER = "_allstak_breadcrumb_patched"


def _sanitize_url(url: str) -> str:
    """Strip query params from URL to avoid leaking sensitive data."""
    return url.split("?")[0] if "?" in url else url


def instrument_requests(add_breadcrumb: Callable[..., None]) -> None:
    """
    Patch ``requests.Session.send`` to add automatic HTTP breadcrumbs.

    Safe to call even if ``requests`` is not installed (no-op).
    """
    try:
        import requests  # type: ignore[import-untyped]
    except ImportError:
        return

    original_send = requests.Session.send

    # Idempotency guard: if our breadcrumb wrapper is already installed, do
    # nothing. Re-initializing the client (or having both this path and the
    # requests integration active) must not double-wrap Session.send.
    if getattr(original_send, _BREADCRUMB_PATCH_MARKER, False):
        return

    @functools.wraps(original_send)
    def patched_send(self: Any, request: Any, **kwargs: Any) -> Any:
        import time

        method = request.method or "GET"
        url = _sanitize_url(request.url or "")
        start = time.monotonic()
        try:
            response = original_send(self, request, **kwargs)
            duration_ms = int((time.monotonic() - start) * 1000)
            add_breadcrumb(
                type="http",
                message=f"{method} {url} -> {response.status_code}",
                level="error" if response.status_code >= 400 else "info",
                data={
                    "method": method,
                    "url": url,
                    "statusCode": response.status_code,
                    "durationMs": duration_ms,
                },
            )
            return response
        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            add_breadcrumb(
                type="http",
                message=f"{method} {url} -> failed: {type(e).__name__}",
                level="error",
                data={
                    "method": method,
                    "url": url,
                    "error": str(e),
                    "durationMs": duration_ms,
                },
            )
            raise

    setattr(patched_send, _BREADCRUMB_PATCH_MARKER, True)
    requests.Session.send = patched_send  # type: ignore[assignment]


def instrument_logging(add_breadcrumb: Callable[..., None]) -> None:
    """
    Add a logging handler that creates breadcrumbs for WARNING+ log messages.

    Avoids infinite recursion by ignoring messages from the ``allstak`` logger
    namespace itself.
    """

    class BreadcrumbHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            # Skip our own SDK logs to prevent infinite recursion
            if record.name.startswith("allstak"):
                return
            # Skip records the dedicated logging integration already turned into
            # a breadcrumb / event, to avoid DOUBLE breadcrumbs.
            from .logging import _ALLSTAK_LOG_HANDLED

            if getattr(record, _ALLSTAK_LOG_HANDLED, False):
                return
            if record.levelno >= logging.WARNING:
                level = "error" if record.levelno >= logging.ERROR else "warn"
                try:
                    add_breadcrumb(
                        type="log",
                        message=record.getMessage(),
                        level=level,
                        data={"logger": record.name, "module": record.module},
                    )
                except Exception:
                    pass

    handler = BreadcrumbHandler()
    handler.setLevel(logging.WARNING)
    logging.getLogger().addHandler(handler)
