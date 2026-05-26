"""Tests for the Celery task-failure integration.

Celery is not a test dependency, so we inject a tiny fake ``celery.signals``
module exposing the same ``Signal`` interface the integration uses
(``.connect(handler, dispatch_uid=..., weak=...)`` and ``.send(...)``).
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

import allstak
from allstak import client as allstak_client


class _FakeSignal:
    """Minimal Django/Celery-style Signal: connect + send."""

    def __init__(self) -> None:
        self._receivers: dict = {}

    def connect(self, handler, dispatch_uid=None, weak=True):
        key = dispatch_uid or id(handler)
        self._receivers[key] = handler

    def send(self, sender=None, **kwargs):
        for handler in list(self._receivers.values()):
            handler(sender=sender, **kwargs)


@pytest.fixture
def fake_celery(monkeypatch):
    """Install a fake ``celery.signals`` module + reset SDK/integration state."""
    signals = types.ModuleType("celery.signals")
    signals.task_prerun = _FakeSignal()
    signals.task_postrun = _FakeSignal()
    signals.task_failure = _FakeSignal()

    celery_mod = types.ModuleType("celery")
    celery_mod.signals = signals

    monkeypatch.setitem(sys.modules, "celery", celery_mod)
    monkeypatch.setitem(sys.modules, "celery.signals", signals)

    # Reset integration + client singletons.
    from allstak.integrations import celery as allstak_celery

    allstak_celery._INSTALLED = False
    allstak_client._client = None
    allstak_client._initialized_once = False

    allstak.init(api_key="ask_test", host="http://allstak.test")

    yield signals, allstak_celery

    allstak_celery._INSTALLED = False
    allstak_client._client = None
    allstak_client._initialized_once = False


def test_task_failure_captures_event_with_context(fake_celery, monkeypatch):
    signals, allstak_celery = fake_celery
    allstak_celery.install_celery()

    captured = {}
    client = allstak.get_client()

    def fake_capture(exc, **kwargs):
        captured["exc"] = exc
        captured["metadata"] = kwargs.get("metadata")
        captured["mechanism"] = kwargs.get("mechanism")
        return "evt-1"

    monkeypatch.setattr(client, "capture_exception", fake_capture)

    sender = MagicMock()
    sender.name = "myapp.tasks.send_email"

    err = ValueError("boom")
    signals.task_failure.send(
        sender=sender,
        task_id="abc-123",
        exception=err,
        args=["positional"],
        kwargs={"to": "x@example.com", "password": "hunter2"},
    )

    assert captured["exc"] is err
    meta = captured["metadata"]
    assert meta["celery.task"] == "myapp.tasks.send_email"
    assert meta["celery.task_id"] == "abc-123"
    assert meta["celery.args"] == ["positional"]
    # PII scrubbing: sensitive kwargs are redacted by the sanitizer.
    assert meta["celery.kwargs"]["to"] == "x@example.com"
    assert meta["celery.kwargs"]["password"] == "[REDACTED]"
    assert captured["mechanism"] == {"type": "celery", "handled": False}


def test_prerun_postrun_span_lifecycle(fake_celery, monkeypatch):
    signals, allstak_celery = fake_celery
    allstak_celery.install_celery()

    client = allstak.get_client()
    crumbs = []
    monkeypatch.setattr(
        client, "add_breadcrumb",
        lambda *a, **k: crumbs.append(k or a),
    )

    task = MagicMock()
    sender = MagicMock()
    sender.name = "myapp.tasks.work"

    signals.task_prerun.send(sender=sender, task_id="t1", task=task)
    # A span was stashed on the task instance.
    assert getattr(task, "_allstak_span", None) is not None
    span = task._allstak_span

    signals.task_postrun.send(sender=sender, task_id="t1", task=task, state="SUCCESS")
    assert span.is_finished is True
    # Span cleaned off the task.
    assert getattr(task, "_allstak_span", None) is None
    # One start breadcrumb was added.
    assert any("started" in str(c) for c in crumbs)


def test_install_celery_is_idempotent(fake_celery):
    signals, allstak_celery = fake_celery
    allstak_celery.install_celery()
    allstak_celery.install_celery()

    # dispatch_uid dedup means exactly one receiver per signal.
    assert len(signals.task_failure._receivers) == 1
    assert len(signals.task_prerun._receivers) == 1
    assert len(signals.task_postrun._receivers) == 1


def test_install_celery_noop_without_celery(monkeypatch):
    """No celery installed -> install_celery is a silent no-op."""
    from allstak.integrations import celery as allstak_celery

    allstak_celery._INSTALLED = False
    monkeypatch.setitem(sys.modules, "celery", None)  # force ImportError
    # Should not raise.
    allstak_celery.install_celery()
    assert allstak_celery._INSTALLED is False
    allstak_celery._INSTALLED = False
