"""
Celery integration for AllStak — captures task failures and (optionally)
wraps each task execution in a span + breadcrumb.

Wires into Celery's signals (no monkey-patching):

1. ``task_failure`` — captures the task's exception as an AllStak error event,
   tagged with the task name, task id, and (PII-scrubbed) args/kwargs.
2. ``task_prerun`` / ``task_postrun`` — start/finish a ``celery.task`` span and
   leave a breadcrumb, so the failure event carries the surrounding context.

Install once at startup::

    import allstak
    from allstak.integrations.celery import install_celery

    allstak.init(api_key="ask_live_...")
    install_celery()

Task args/kwargs are routed through the SDK sanitizer
(:func:`allstak.sanitize.scrub`), so values for sensitive keys
(``password``, ``token``, ...) never leave the worker.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("allstak.sdk")

_INSTALLED = False


def install_celery() -> None:
    """
    Connect AllStak handlers to Celery's task signals. Idempotent and a safe
    no-op if Celery is not installed.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    try:
        from celery import signals  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("celery not installed — skipping celery instrumentation")
        return

    # ``dispatch_uid`` makes Celery's signal registry deduplicate connections,
    # so even if the idempotency guard is bypassed we never double-subscribe.
    signals.task_prerun.connect(_on_task_prerun, dispatch_uid="allstak.celery.prerun", weak=False)
    signals.task_postrun.connect(_on_task_postrun, dispatch_uid="allstak.celery.postrun", weak=False)
    signals.task_failure.connect(_on_task_failure, dispatch_uid="allstak.celery.failure", weak=False)

    _INSTALLED = True
    logger.info("AllStak celery auto-instrumentation installed")


def _task_name(sender: Any, task: Any) -> str:
    name = getattr(sender, "name", None) or getattr(task, "name", None)
    return str(name) if name else "celery.task"


def _scrub(value: Any) -> Any:
    """Route task args/kwargs through the SDK sanitizer before they leave the worker."""
    try:
        from ..sanitize import scrub

        return scrub(value)
    except Exception:
        return None


def _on_task_prerun(
    sender: Any = None,
    task_id: Optional[str] = None,
    task: Any = None,
    args: Any = None,
    kwargs: Any = None,
    **extra: Any,
) -> None:
    try:
        import allstak

        client = allstak.get_client()
        if client is None:
            return

        name = _task_name(sender, task)
        span = client.start_span(
            "celery.task",
            description=name,
            tags={"celery.task": name, "celery.task_id": str(task_id or "")},
        )
        # Stash the span on the task instance so postrun/failure can finish it.
        if task is not None:
            try:
                task._allstak_span = span
            except Exception:
                pass

        client.add_breadcrumb(
            type="task",
            message=f"celery task started: {name}",
            level="info",
            data={"task": name, "taskId": str(task_id or "")},
        )
    except Exception as e:  # never break the worker over instrumentation
        logger.debug("allstak celery prerun hook failed: %s", e)


def _on_task_postrun(
    sender: Any = None,
    task_id: Optional[str] = None,
    task: Any = None,
    retval: Any = None,
    state: Optional[str] = None,
    **extra: Any,
) -> None:
    try:
        span = getattr(task, "_allstak_span", None) if task is not None else None
        if span is not None:
            try:
                span.set_tag("celery.state", str(state or ""))
                span.finish("error" if state == "FAILURE" else "ok")
            except Exception:
                pass
            try:
                delattr(task, "_allstak_span")
            except Exception:
                pass
    except Exception as e:
        logger.debug("allstak celery postrun hook failed: %s", e)


def _on_task_failure(
    sender: Any = None,
    task_id: Optional[str] = None,
    exception: Optional[BaseException] = None,
    args: Any = None,
    kwargs: Any = None,
    traceback: Any = None,
    einfo: Any = None,
    **extra: Any,
) -> None:
    try:
        import allstak

        client = allstak.get_client()
        if client is None or exception is None:
            return

        name = _task_name(sender, getattr(sender, "task", None))

        metadata: Dict[str, Any] = {
            "celery.task": name,
            "celery.task_id": str(task_id or ""),
            "celery.args": _scrub(list(args)) if args is not None else None,
            "celery.kwargs": _scrub(dict(kwargs)) if kwargs is not None else None,
        }
        # Drop keys with no value so the event metadata stays clean.
        metadata = {k: v for k, v in metadata.items() if v is not None}

        client.capture_exception(
            exception,
            metadata=metadata,
            mechanism={"type": "celery", "handled": False},
        )
    except Exception as e:  # never break the worker over instrumentation
        logger.debug("allstak celery failure hook failed: %s", e)
