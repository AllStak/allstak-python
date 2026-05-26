"""Tests for global uncaught-exception capture (sys/threading excepthook)."""

import sys
import threading

import pytest

from allstak import excepthook


@pytest.fixture(autouse=True)
def _clean_hooks():
    """Snapshot and restore the real hooks around every test."""
    orig_sys = sys.excepthook
    orig_thread = getattr(threading, "excepthook", None)
    # Ensure no leftover install from a previous test.
    excepthook.uninstall()
    yield
    excepthook.uninstall()
    sys.excepthook = orig_sys
    if orig_thread is not None:
        threading.excepthook = orig_thread


def test_install_replaces_and_chains_sys_excepthook():
    called = {"prev": False}

    def prev_hook(exc_type, exc_value, exc_tb):
        called["prev"] = True

    sys.excepthook = prev_hook

    captured = []

    class FakeClient:
        def capture_exception(self, exc, **kwargs):
            captured.append((exc, kwargs))

        def flush(self):
            pass

    excepthook.install(lambda: FakeClient(), install_threading=False)
    assert excepthook.is_installed()
    assert sys.excepthook is not prev_hook

    err = ValueError("boom")
    sys.excepthook(type(err), err, None)

    # Our hook captured it and tagged it unhandled...
    assert len(captured) == 1
    exc, kwargs = captured[0]
    assert exc is err
    assert kwargs["mechanism"] == {"type": "excepthook", "handled": False}
    # ...and chained the previous hook (did not swallow).
    assert called["prev"] is True


def test_install_is_idempotent():
    def client_getter():
        return None

    excepthook.install(client_getter, install_threading=False)
    hook_after_first = sys.excepthook
    # Second install must be a no-op — does not re-wrap.
    excepthook.install(client_getter, install_threading=False)
    assert sys.excepthook is hook_after_first


def test_uninstall_restores_previous_hook():
    def prev_hook(exc_type, exc_value, exc_tb):
        pass

    sys.excepthook = prev_hook
    excepthook.install(lambda: None, install_threading=False)
    assert sys.excepthook is not prev_hook
    excepthook.uninstall()
    assert sys.excepthook is prev_hook
    assert not excepthook.is_installed()


def test_keyboard_interrupt_is_not_captured_but_chained():
    captured = []
    chained = {"hit": False}

    def prev_hook(exc_type, exc_value, exc_tb):
        chained["hit"] = True

    sys.excepthook = prev_hook

    class FakeClient:
        def capture_exception(self, exc, **kwargs):
            captured.append(exc)

        def flush(self):
            pass

    excepthook.install(lambda: FakeClient(), install_threading=False)
    kb = KeyboardInterrupt()
    sys.excepthook(type(kb), kb, None)

    assert captured == []  # never capture KeyboardInterrupt
    assert chained["hit"] is True  # still chained


def test_threading_excepthook_captures_and_chains():
    if not hasattr(threading, "excepthook"):
        pytest.skip("threading.excepthook requires Python 3.8+")

    chained = {"hit": False}
    orig = threading.excepthook

    def prev_hook(args):
        chained["hit"] = True

    threading.excepthook = prev_hook

    captured = []

    class FakeClient:
        def capture_exception(self, exc, **kwargs):
            captured.append((exc, kwargs))

        def flush(self):
            pass

    excepthook.install(lambda: FakeClient(), install_sys=False)

    err = RuntimeError("thread boom")

    class Args:
        exc_type = RuntimeError
        exc_value = err
        exc_traceback = None
        thread = None

    threading.excepthook(Args())

    assert len(captured) == 1
    exc, kwargs = captured[0]
    assert exc is err
    assert kwargs["mechanism"] == {"type": "threading_excepthook", "handled": False}
    assert chained["hit"] is True

    threading.excepthook = orig
