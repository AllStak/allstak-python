"""Tests for the logging -> error-event integration."""
from __future__ import annotations

import logging

import pytest

import allstak
from allstak import client as allstak_client
from allstak.integrations.logging import (
    AllStakLoggingHandler,
    install_logging,
    _HANDLER_MARKER,
    _ALLSTAK_LOG_HANDLED,
)


@pytest.fixture
def sdk(monkeypatch):
    allstak_client._client = None
    allstak_client._initialized_once = False
    allstak.init(api_key="ask_test", host="http://allstak.test")
    client = allstak.get_client()

    events = []
    crumbs = []
    monkeypatch.setattr(
        client, "capture_exception",
        lambda exc, **k: events.append(("exc", exc, k)) or "evt",
    )
    monkeypatch.setattr(
        client, "capture_error",
        lambda cls, msg, **k: events.append(("error", cls, msg, k)) or "evt",
    )
    monkeypatch.setattr(
        client, "add_breadcrumb",
        lambda **k: crumbs.append(k),
    )

    # Clean root logger handlers we add, restore afterwards.
    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level

    yield client, events, crumbs

    root.handlers = saved
    root.setLevel(saved_level)
    allstak_client._client = None
    allstak_client._initialized_once = False


def test_error_record_becomes_captured_event(sdk):
    client, events, crumbs = sdk
    install_logging(level=logging.ERROR, breadcrumb_level=logging.INFO)

    log = logging.getLogger("myapp")
    try:
        raise RuntimeError("kaboom")
    except RuntimeError:
        log.exception("something failed")

    # Exactly one error event, carrying the exception + stack.
    exc_events = [e for e in events if e[0] == "exc"]
    assert len(exc_events) == 1
    assert isinstance(exc_events[0][1], RuntimeError)
    assert exc_events[0][2]["mechanism"] == {"type": "logging", "handled": True}
    # No breadcrumb for an event-level record.
    assert crumbs == []


def test_error_without_exc_info_uses_capture_error(sdk):
    client, events, crumbs = sdk
    install_logging(level=logging.ERROR, breadcrumb_level=logging.INFO)

    logging.getLogger("myapp").error("plain error message")

    err_events = [e for e in events if e[0] == "error"]
    assert len(err_events) == 1
    assert err_events[0][2] == "plain error message"


def test_info_record_is_breadcrumb_only(sdk):
    client, events, crumbs = sdk
    install_logging(level=logging.ERROR, breadcrumb_level=logging.INFO)

    logging.getLogger("myapp").info("just fyi")

    assert events == []  # not an event
    assert len(crumbs) == 1
    assert crumbs[0]["type"] == "log"
    assert crumbs[0]["message"] == "just fyi"


def test_no_double_breadcrumb_with_auto_breadcrumb_handler(sdk):
    """An INFO record handled by our handler must NOT be re-crumbed by the
    auto-breadcrumb WARNING handler."""
    client, events, crumbs = sdk
    from allstak.integrations.auto_breadcrumbs import instrument_logging

    # Auto-breadcrumb handler (WARNING+) plus our handler (INFO breadcrumb / ERROR event).
    instrument_logging(client.add_breadcrumb)
    install_logging(level=logging.ERROR, breadcrumb_level=logging.WARNING)

    logging.getLogger("myapp").warning("heads up")

    # Only one breadcrumb: ours stamped the record so auto-breadcrumb skipped it.
    log_crumbs = [c for c in crumbs if c.get("type") == "log"]
    assert len(log_crumbs) == 1


def test_install_logging_is_idempotent(sdk):
    client, events, crumbs = sdk
    root = logging.getLogger()

    install_logging()
    install_logging()
    install_logging(level=logging.CRITICAL)

    ours = [h for h in root.handlers if getattr(h, _HANDLER_MARKER, False)]
    assert len(ours) == 1
    assert isinstance(ours[0], AllStakLoggingHandler)
    # Last call's level wins.
    assert ours[0].event_level == logging.CRITICAL


def test_skips_own_sdk_logs(sdk):
    client, events, crumbs = sdk
    install_logging(level=logging.ERROR, breadcrumb_level=logging.INFO)

    logging.getLogger("allstak.sdk").error("internal noise")

    assert events == []
    assert crumbs == []
