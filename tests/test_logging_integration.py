"""Tests for the logging -> error-event integration."""
from __future__ import annotations

import logging

import pytest

import allstak
from allstak import client as allstak_client
from allstak.integrations.logging import (
    AllStakLoggingHandler,
    install_logging,
    mark_exception_captured,
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


# ---------------------------------------------------------------------------
# Auto-attach from init() gated by capture_logs (default-on)
# ---------------------------------------------------------------------------

def _ours_on_root() -> list:
    root = logging.getLogger()
    return [h for h in root.handlers if getattr(h, _HANDLER_MARKER, False)]


def test_init_auto_attaches_logging_bridge_by_default(monkeypatch):
    """init() with the default capture_logs=True attaches the bridge handler."""
    root = logging.getLogger()
    saved, saved_level = list(root.handlers), root.level
    allstak_client._client = None
    allstak_client._initialized_once = False
    try:
        allstak.init(api_key="ask_test", host="http://allstak.test")
        ours = _ours_on_root()
        assert len(ours) == 1
        assert isinstance(ours[0], AllStakLoggingHandler)
        assert ours[0].event_level == logging.ERROR
    finally:
        root.handlers = saved
        root.setLevel(saved_level)
        allstak_client._client = None
        allstak_client._initialized_once = False


def test_init_capture_logs_false_does_not_attach(monkeypatch):
    root = logging.getLogger()
    saved, saved_level = list(root.handlers), root.level
    allstak_client._client = None
    allstak_client._initialized_once = False
    try:
        allstak.init(
            api_key="ask_test", host="http://allstak.test", capture_logs=False
        )
        assert _ours_on_root() == []
    finally:
        root.handlers = saved
        root.setLevel(saved_level)
        allstak_client._client = None
        allstak_client._initialized_once = False


def test_init_capture_logs_level_overrides(monkeypatch):
    root = logging.getLogger()
    saved, saved_level = list(root.handlers), root.level
    allstak_client._client = None
    allstak_client._initialized_once = False
    try:
        allstak.init(
            api_key="ask_test",
            host="http://allstak.test",
            capture_logs_level=logging.CRITICAL,
            capture_logs_breadcrumb_level=logging.WARNING,
        )
        ours = _ours_on_root()
        assert len(ours) == 1
        assert ours[0].event_level == logging.CRITICAL
        assert ours[0].breadcrumb_level == logging.WARNING
    finally:
        root.handlers = saved
        root.setLevel(saved_level)
        allstak_client._client = None
        allstak_client._initialized_once = False


# ---------------------------------------------------------------------------
# FATAL/CRITICAL promotion + trace/request stamping
# ---------------------------------------------------------------------------

def test_critical_record_promoted_to_fatal_level(sdk):
    client, events, crumbs = sdk
    install_logging(level=logging.ERROR, breadcrumb_level=logging.INFO)

    logging.getLogger("myapp").critical("the sky is falling")

    err_events = [e for e in events if e[0] == "error"]
    assert len(err_events) == 1
    # capture_error(cls, msg, **kwargs) -> kwargs is index 3 in the spy tuple.
    assert err_events[0][3]["level"] == "fatal"


def test_critical_with_exc_info_promoted_to_fatal_exception(sdk):
    client, events, crumbs = sdk
    install_logging(level=logging.ERROR, breadcrumb_level=logging.INFO)

    log = logging.getLogger("myapp")
    try:
        raise RuntimeError("fatal boom")
    except RuntimeError:
        log.critical("crashed hard", exc_info=True)

    exc_events = [e for e in events if e[0] == "exc"]
    assert len(exc_events) == 1
    assert isinstance(exc_events[0][1], RuntimeError)
    assert exc_events[0][2]["level"] == "fatal"


def test_event_metadata_stamps_trace_id(sdk):
    client, events, crumbs = sdk
    install_logging(level=logging.ERROR, breadcrumb_level=logging.INFO)

    # Seed an active trace so the bridge can correlate the log to it.
    client.set_trace_id("a" * 32)
    logging.getLogger("myapp").error("with trace")

    err_events = [e for e in events if e[0] == "error"]
    assert len(err_events) == 1
    meta = err_events[0][3]["metadata"]
    assert meta["traceId"] == "a" * 32


def test_event_metadata_stamps_request_id_from_record(sdk):
    client, events, crumbs = sdk
    install_logging(level=logging.ERROR, breadcrumb_level=logging.INFO)

    # A request-scoped logging filter pattern: record.request_id attribute.
    logging.getLogger("myapp").error("scoped", extra={"request_id": "req-123"})

    err_events = [e for e in events if e[0] == "error"]
    assert len(err_events) == 1
    assert err_events[0][3]["metadata"]["requestId"] == "req-123"


# ---------------------------------------------------------------------------
# Dedup: an exception already reported by a framework integration is skipped
# ---------------------------------------------------------------------------

def test_already_captured_exception_is_not_double_reported(sdk):
    client, events, crumbs = sdk
    install_logging(level=logging.ERROR, breadcrumb_level=logging.INFO)

    log = logging.getLogger("myapp")
    try:
        raise ValueError("already handled by the framework")
    except ValueError as exc:
        # Simulate a framework integration having already captured it.
        mark_exception_captured(exc)
        log.exception("framework will re-log this")

    # No new error event — the bridge skipped the already-captured exception.
    assert [e for e in events if e[0] == "exc"] == []
    assert [e for e in events if e[0] == "error"] == []
