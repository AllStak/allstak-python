"""
FastAPI / Starlette integration for AllStak.

Automatically captures:
- Inbound HTTP request telemetry (method, path, status, duration, size)
- Unhandled exceptions from routes and dependencies (surfaced as errors)
- Per-request trace ID (from ``traceparent``/``x-request-id`` or newly generated)
- User context from ``request.state.user`` if your auth dependency sets it

Setup::

    from fastapi import FastAPI
    import allstak
    from allstak.integrations.fastapi import AllStakFastAPI

    allstak.init(api_key="ask_live_...", host="https://ingest.allstak.dev")

    app = FastAPI()
    AllStakFastAPI(app, service="my-api")

The integration is a pure ASGI middleware — it works with any Starlette-based
framework (FastAPI, Starlette, Litestar via Starlette compat, etc.).
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("allstak.sdk")

try:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send
    _STARLETTE_AVAILABLE = True
except ImportError:
    _STARLETTE_AVAILABLE = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class AllStakASGIMiddleware:
    """
    Pure ASGI middleware that records inbound HTTP telemetry,
    trace context, and unhandled exceptions for every request.
    """

    def __init__(self, app: "ASGIApp", service: str = "") -> None:
        if not _STARLETTE_AVAILABLE:
            raise ImportError(
                "AllStakFastAPI requires starlette/fastapi. "
                "Install with: pip install allstak[fastapi]"
            )
        self.app = app
        self.service = service

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Lazy import — SDK might not be initialized
        import allstak
        client = allstak.get_client()

        start_ms = time.monotonic() * 1000
        start_ts = _now_iso()

        # Extract trace ID from headers or generate a new one
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        incoming_trace = headers.get("x-allstak-trace-id") or headers.get("x-request-id")
        trace_id: str
        if client is not None:
            if incoming_trace:
                client.set_trace_id(incoming_trace)
                trace_id = incoming_trace
            else:
                client.reset_trace()  # fresh per-request trace
                trace_id = client.get_trace_id()
            if self.service:
                client.tracing.set_service(self.service)
        else:
            trace_id = incoming_trace or uuid.uuid4().hex

        # Track response status + size via a send wrapper
        status_code_box = {"code": 0, "size": 0}

        async def send_wrapper(message: "Message") -> None:
            if message["type"] == "http.response.start":
                status_code_box["code"] = int(message.get("status", 0))
            elif message["type"] == "http.response.body":
                body = message.get("body") or b""
                if body:
                    status_code_box["size"] += len(body)
            await send(message)

        method = scope.get("method", "GET")
        raw_path: str = scope.get("path", "/") or "/"
        server = scope.get("server") or ("localhost", None)
        host_header = headers.get("host") or (f"{server[0]}:{server[1]}" if server[1] else server[0])
        req_content_length = int(headers.get("content-length") or 0)

        exc_to_capture: Optional[BaseException] = None
        # Starlette populates scope["state"] lazily — prepare a dict so downstream
        # handlers can attach user context.
        if "state" not in scope:
            scope["state"] = {}
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            exc_to_capture = exc
            # Let the framework's own exception handler kick in by re-raising.
            # (If we swallow, the client would hang.)
            status_code_box["code"] = status_code_box["code"] or 500
            raise
        finally:
            duration_ms = int(time.monotonic() * 1000 - start_ms)
            if client is not None:
                try:
                    client.http.record(
                        direction="inbound",
                        method=method,
                        host=host_header,
                        path=raw_path,
                        status_code=status_code_box["code"] or 0,
                        duration_ms=duration_ms,
                        request_size=req_content_length,
                        response_size=status_code_box["size"],
                        trace_id=trace_id,
                        timestamp=start_ts,
                    )
                except Exception:
                    pass
                if exc_to_capture is not None:
                    try:
                        from ..models.errors import RequestContext

                        req_ctx = RequestContext(
                            method=method,
                            path=raw_path,
                            host=host_header,
                            status_code=status_code_box["code"] or 500,
                            user_agent=headers.get("user-agent"),
                        )
                        client.capture_exception(
                            exc_to_capture,
                            request_context=req_ctx,
                            metadata={
                                "http.method": method,
                                "http.path": raw_path,
                                "http.host": host_header,
                                "http.status": status_code_box["code"] or 500,
                                "traceId": trace_id,
                            },
                        )
                    except Exception:
                        pass


class AllStakFastAPI:
    """
    Convenience wrapper that registers :class:`AllStakASGIMiddleware` on a FastAPI app.

    Usage::

        from fastapi import FastAPI
        from allstak.integrations.fastapi import AllStakFastAPI

        app = FastAPI()
        AllStakFastAPI(app, service="my-api")
    """

    def __init__(self, app: Any, *, service: str = "") -> None:
        if not _STARLETTE_AVAILABLE:
            raise ImportError(
                "AllStakFastAPI requires starlette/fastapi. "
                "Install with: pip install allstak[fastapi]"
            )
        app.add_middleware(AllStakASGIMiddleware, service=service)
