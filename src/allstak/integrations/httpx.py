"""
httpx integration for AllStak — auto-captures every outbound request.

Wires httpx's native event hooks (added in httpx 0.20+) so every
``httpx.Client`` / ``httpx.AsyncClient`` request that the application makes
shows up on the AllStak Requests dashboard, with no per-call instrumentation.

Two install paths:

1. Global — call once at startup. Patches ``httpx.Client.__init__`` and
   ``httpx.AsyncClient.__init__`` so EVERY client (existing or future) gets
   the AllStak event hooks::

       from allstak.integrations.httpx import install_httpx
       install_httpx()

2. Per-client — when you don't want to monkey-patch, attach hooks to a
   specific client::

       client = httpx.AsyncClient(event_hooks=allstak_httpx_hooks())

Both paths skip requests to AllStak's own ingest endpoints to avoid
recursion.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List

logger = logging.getLogger("allstak.sdk")

_INSTALLED = False


def _build_hooks(allstak_host: str | None) -> Dict[str, List[Callable[..., Any]]]:
    """
    Returns httpx event_hooks dict that records each outbound request.
    Skips requests whose URL starts with the AllStak ingest host.
    """
    # Imported lazily so importing this module doesn't require httpx.
    from .. import get_client

    def _on_request(request: Any) -> None:
        # httpx 0.20+: request.extensions is a free-form dict; we use it to
        # carry our own start time across the request/response pair.
        request.extensions["allstak_start"] = time.monotonic()

    def _on_response(response: Any) -> None:
        client = get_client()
        if client is None:
            return
        request = response.request
        url = str(request.url)
        if allstak_host and url.startswith(allstak_host):
            return  # don't capture our own ingest traffic

        start = request.extensions.get("allstak_start")
        duration_ms = int((time.monotonic() - start) * 1000) if start else 0

        try:
            host = request.url.host
            port = request.url.port
            host_with_port = f"{host}:{port}" if port else host
        except Exception:
            host_with_port = ""

        try:
            client.http.record(
                direction="outbound",
                method=request.method.upper(),
                host=host_with_port,
                path=str(request.url.path) or "/",
                status_code=response.status_code,
                duration_ms=duration_ms,
            )
        except Exception as e:  # never break the host application
            logger.debug("allstak httpx capture failed: %s", e)

    return {"request": [_on_request], "response": [_on_response]}


def allstak_httpx_hooks() -> Dict[str, List[Callable[..., Any]]]:
    """Return event_hooks suitable to pass to ``httpx.Client(event_hooks=...)``."""
    from .. import get_client
    c = get_client()
    host = getattr(c.config, "host", None) if c else None
    return _build_hooks(host)


def install_httpx() -> None:
    """
    Globally instrument httpx by patching ``Client.__init__`` /
    ``AsyncClient.__init__`` to inject AllStak event hooks. Idempotent.

    Safe no-op if ``httpx`` is not importable.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    try:
        import httpx  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("httpx not installed — skipping httpx instrumentation")
        return

    from .. import get_client

    for cls_name in ("Client", "AsyncClient"):
        cls = getattr(httpx, cls_name, None)
        if cls is None:
            continue

        original_init = cls.__init__

        def make_patched(orig: Any, _cls_name: str) -> Any:
            def patched(self: Any, *args: Any, **kwargs: Any) -> None:
                client = get_client()
                allstak_host = getattr(client._config, "host", None) if client else None
                hooks = _build_hooks(allstak_host)
                user_hooks = kwargs.pop("event_hooks", {}) or {}
                # Merge (don't replace) the user's hooks.
                merged: Dict[str, List[Callable[..., Any]]] = {}
                for k in {"request", "response"}:
                    merged[k] = list(user_hooks.get(k, [])) + list(hooks.get(k, []))
                kwargs["event_hooks"] = merged
                orig(self, *args, **kwargs)
            return patched

        cls.__init__ = make_patched(original_init, cls_name)  # type: ignore[method-assign]

    _INSTALLED = True
    logger.info("AllStak httpx auto-instrumentation installed")
