"""Tests for the import-time FastAPI / Starlette auto-instrument shim.

``allstak.init(capture_fastapi=True)`` patches Starlette's middleware-stack
builder so every FastAPI / Starlette app auto-attaches
:class:`AllStakASGIMiddleware` with no ``AllStakFastAPI(app)`` line. These tests
drive a real FastAPI app through the Starlette TestClient and assert the
integration reports inbound telemetry by spying on the public client surface.

The autouse conftest fixture restores ``Starlette.build_middleware_stack`` after
each test, so the global patch never leaks across suites.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import allstak
from allstak import client as allstak_client
from allstak.integrations.fastapi import (
    AllStakASGIMiddleware,
    AllStakFastAPI,
    _app_already_has_middleware,
)


def _spy_client(client):
    """Replace the reporting surface with spies; collect inbound http.records."""
    http_record = MagicMock(return_value=None)
    client.http.record = http_record
    real_start_span = client.start_span
    spans = []

    def _start_span(operation, *, description="", tags=None):
        span = real_start_span(operation, description=description, tags=tags)
        if operation == "http.server":
            spans.append(span)
        return span

    client.start_span = _start_span

    def inbound():
        return [
            c for c in http_record.call_args_list
            if c.kwargs.get("direction") == "inbound"
        ]

    return http_record, spans, inbound


@pytest.fixture
def initialized():
    allstak_client._client = None
    allstak_client._initialized_once = False
    allstak.init(api_key="ask_test", host="http://allstak.test")
    yield allstak.get_client()
    allstak_client._client = None
    allstak_client._initialized_once = False


def test_autoinstrument_attaches_without_manual_wrapper(initialized):
    client = initialized
    http_record, spans, inbound = _spy_client(client)

    # NO AllStakFastAPI(app) — the shim must wire it on first request.
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    cl = TestClient(app, raise_server_exceptions=False)
    resp = cl.get("/ping")
    assert resp.status_code == 200

    # Auto-attached, recorded inbound telemetry, opened an http.server span.
    assert _app_already_has_middleware(app) is True
    assert len(inbound()) >= 1
    assert inbound()[0].kwargs["method"] == "GET"
    assert any(s for s in spans)


def test_autoinstrument_does_not_double_wrap_manual_app(initialized):
    client = initialized
    http_record, spans, inbound = _spy_client(client)

    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    # Manual wrapper + the import-time shim both active: must not double-attach.
    AllStakFastAPI(app, service="manual")

    cl = TestClient(app, raise_server_exceptions=False)
    resp = cl.get("/ping")
    assert resp.status_code == 200

    # Exactly one AllStak middleware on the stack and exactly one inbound record.
    allstak_mw = [
        mw for mw in app.user_middleware if getattr(mw, "cls", None) is AllStakASGIMiddleware
    ]
    assert len(allstak_mw) == 1
    assert len(inbound()) == 1


def test_autoinstrument_noop_when_sdk_not_initialized():
    # No client at all → the patched builder must be a true no-op.
    allstak_client._client = None
    allstak_client._initialized_once = False

    # Install the patch as init() would, but with no live client.
    from allstak.integrations.fastapi import autoinstrument

    autoinstrument()

    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    cl = TestClient(app, raise_server_exceptions=False)
    assert cl.get("/ping").status_code == 200
    # No AllStak middleware attached because no client was initialized.
    assert _app_already_has_middleware(app) is False


def test_autoinstrument_is_idempotent(initialized):
    from allstak.integrations.fastapi import autoinstrument

    # Re-calling does not stack patches; still reports True (patch in place).
    assert autoinstrument() is True
    assert autoinstrument() is True
