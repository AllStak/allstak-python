"""
Global uncaught-exception capture.

Installs ``sys.excepthook`` (main thread) and ``threading.excepthook``
(background threads, Python 3.8+) so exceptions that escape all
application code — outside any framework request/middleware — are still
captured by AllStak before the process terminates.

Design rules:

* **Never swallow.** The previous hook is always chained (called after we
  capture), so the interpreter still prints the traceback / terminates
  exactly as it would have without AllStak installed.
* **Idempotent.** Installing twice is a no-op; the original hooks are
  stored so they can be restored.
* **Best-effort flush.** Because the process is dying, we attempt a
  synchronous transport flush with a short timeout and never hang.
* **Unhandled mechanism.** Captured events are tagged
  ``{"type": "excepthook"/"threading_excepthook", "handled": False}``.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger("allstak.sdk")

# Time we are willing to block on a best-effort flush while the process dies.
_FLUSH_TIMEOUT_SECONDS = 2.0

# State for idempotent install / restore. Guarded by _lock.
_lock = threading.Lock()
_installed = False
_prev_excepthook: Optional[Callable[..., Any]] = None
_prev_threading_excepthook: Optional[Callable[..., Any]] = None
# Whether each individual hook was actually installed (so we restore precisely).
_installed_sys = False
_installed_threading = False


def _best_effort_flush(client: Any) -> None:
    """Flush the client synchronously with a hard timeout — never hang.

    The interpreter is shutting down, so we run ``client.flush()`` on a
    daemon thread and join for at most ``_FLUSH_TIMEOUT_SECONDS``. If the
    flush is slow (network), we give up rather than block process exit.
    """
    if client is None:
        return
    try:
        worker = threading.Thread(
            target=lambda: _safe_flush(client),
            name="allstak-excepthook-flush",
            daemon=True,
        )
        worker.start()
        worker.join(timeout=_FLUSH_TIMEOUT_SECONDS)
    except Exception as exc:  # pragma: no cover — flush must never raise here
        logger.debug("[AllStak] excepthook flush failed: %s", exc)


def _safe_flush(client: Any) -> None:
    try:
        client.flush()
    except Exception as exc:  # pragma: no cover
        logger.debug("[AllStak] excepthook flush worker failed: %s", exc)


def _capture(
    exc: BaseException,
    *,
    mechanism_type: str,
    client_getter: Callable[[], Any],
) -> None:
    """Capture an uncaught exception, tagged as unhandled. Never raises."""
    try:
        client = client_getter()
        if client is None:
            return
        client.capture_exception(
            exc,
            level="error",
            mechanism={"type": mechanism_type, "handled": False},
        )
        _best_effort_flush(client)
    except Exception as cap_err:  # pragma: no cover — must not break teardown
        logger.debug("[AllStak] excepthook capture failed: %s", cap_err)


def install(
    client_getter: Callable[[], Any],
    *,
    install_sys: bool = True,
    install_threading: bool = True,
) -> None:
    """Install global exception hooks. Idempotent.

    :param client_getter: callable returning the current AllStak client (or
        ``None``). Resolved lazily at exception time, not install time.
    :param install_sys: install ``sys.excepthook`` for the main thread.
    :param install_threading: install ``threading.excepthook`` for threads.
    """
    global _installed, _prev_excepthook, _prev_threading_excepthook
    global _installed_sys, _installed_threading

    with _lock:
        if _installed:
            # Already installed once — guard against double-install on re-init.
            return

        if install_sys:
            _prev_excepthook = sys.excepthook

            def allstak_excepthook(
                exc_type: type, exc_value: BaseException, exc_tb: Any
            ) -> None:
                if exc_value is not None and not issubclass(
                    exc_type, (KeyboardInterrupt, SystemExit)
                ):
                    _capture(
                        exc_value,
                        mechanism_type="excepthook",
                        client_getter=client_getter,
                    )
                # Always chain the previous hook — never swallow.
                prev = _prev_excepthook
                try:
                    if prev is not None:
                        prev(exc_type, exc_value, exc_tb)
                except Exception:  # pragma: no cover
                    sys.__excepthook__(exc_type, exc_value, exc_tb)

            sys.excepthook = allstak_excepthook
            _installed_sys = True

        if install_threading and hasattr(threading, "excepthook"):
            _prev_threading_excepthook = threading.excepthook

            def allstak_threading_excepthook(args: Any) -> None:
                exc_value = getattr(args, "exc_value", None)
                exc_type = getattr(args, "exc_type", None)
                # threading reports SystemExit but we ignore it like the stdlib.
                if (
                    exc_value is not None
                    and exc_type is not None
                    and not issubclass(exc_type, (KeyboardInterrupt, SystemExit))
                ):
                    _capture(
                        exc_value,
                        mechanism_type="threading_excepthook",
                        client_getter=client_getter,
                    )
                # Always chain the previous hook — never swallow.
                prev = _prev_threading_excepthook
                try:
                    if prev is not None:
                        prev(args)
                except Exception:  # pragma: no cover
                    pass

            threading.excepthook = allstak_threading_excepthook  # type: ignore[assignment]
            _installed_threading = True

        _installed = True


def uninstall() -> None:
    """Restore the previously-installed hooks. Idempotent.

    Primarily for tests and clean teardown.
    """
    global _installed, _prev_excepthook, _prev_threading_excepthook
    global _installed_sys, _installed_threading

    with _lock:
        if not _installed:
            return
        if _installed_sys and _prev_excepthook is not None:
            sys.excepthook = _prev_excepthook
        if (
            _installed_threading
            and _prev_threading_excepthook is not None
            and hasattr(threading, "excepthook")
        ):
            threading.excepthook = _prev_threading_excepthook  # type: ignore[assignment]
        _installed = False
        _installed_sys = False
        _installed_threading = False
        _prev_excepthook = None
        _prev_threading_excepthook = None


def is_installed() -> bool:
    """Whether the AllStak global hooks are currently installed."""
    return _installed
