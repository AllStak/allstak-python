"""
``requests`` integration for AllStak — auto-captures and trace-propagates every
outbound request made via the popular synchronous ``requests`` library.

Patches ``requests.sessions.Session.send`` (which every ``requests`` call and
every ``Session`` ultimately goes through) so that, with no per-call changes:

1. Outbound requests carry the AllStak distributed-trace headers
   (``traceparent`` + ``baggage`` + ``x-allstak-*``), continuing the current
   trace into the downstream service.
2. Each request is recorded on the AllStak Requests dashboard.

Install once at startup::

    from allstak.integrations.requests import install_requests
    install_requests()

Requests to AllStak's own ingest host are skipped (no recursion, no headers).
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

from ..propagation import set_mapping_headers

logger = logging.getLogger("allstak.sdk")

_INSTALLED = False


def install_requests() -> None:
    """
    Globally instrument the ``requests`` library by patching
    ``Session.send``. Idempotent and a safe no-op if ``requests`` is not
    installed.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    try:
        import requests  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("requests not installed — skipping requests instrumentation")
        return

    from .. import get_client

    original_send = requests.sessions.Session.send

    def patched_send(self: Any, request: Any, **kwargs: Any) -> Any:
        client = get_client()
        url = getattr(request, "url", "") or ""
        allstak_host = getattr(getattr(client, "_config", None), "host", None) if client else None
        is_own = bool(allstak_host and url.startswith(allstak_host))

        trace_id = None
        request_id = None
        span = None
        start = time.monotonic()

        if client is not None and not is_own:
            try:
                trace_id = client.get_trace_id()
                if trace_id:
                    request_id = uuid.uuid4().hex
                    method = (getattr(request, "method", "GET") or "GET").upper()
                    path = urlsplit(url).path or "/"
                    span = client.start_span(
                        "http.client",
                        description=f"{method} {path}",
                        tags={"http.method": method, "http.url": url},
                    )
                    set_mapping_headers(
                        request.headers,
                        trace_id=trace_id,
                        request_id=request_id,
                        span_id=span.span_id,
                        overwrite=False,
                    )
            except Exception as e:  # never break the host request over instrumentation
                logger.debug("allstak requests send hook failed: %s", e)

        try:
            response = original_send(self, request, **kwargs)
        except Exception:
            if span is not None:
                try:
                    span.finish("error")
                except Exception:
                    pass
            raise

        if client is not None and not is_own:
            try:
                duration_ms = int((time.monotonic() - start) * 1000)
                parts = urlsplit(url)
                host = parts.netloc or ""
                status = getattr(response, "status_code", 0) or 0
                client.http.record(
                    direction="outbound",
                    method=(getattr(request, "method", "GET") or "GET").upper(),
                    host=host,
                    path=parts.path or "/",
                    status_code=status,
                    duration_ms=duration_ms,
                    trace_id=trace_id,
                    request_id=request_id,
                    span_id=getattr(span, "span_id", None),
                )
                if span is not None:
                    span.set_tag("http.status_code", str(status))
                    span.finish("error" if status >= 500 else "ok")
            except Exception as e:  # never break the host application
                logger.debug("allstak requests capture failed: %s", e)

        return response

    requests.sessions.Session.send = patched_send  # type: ignore[method-assign]
    _INSTALLED = True
    logger.info("AllStak requests auto-instrumentation installed")
