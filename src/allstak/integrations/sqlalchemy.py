"""
SQLAlchemy integration for AllStak.

Wires into SQLAlchemy's ``before_cursor_execute`` / ``after_cursor_execute``
events so every ORM or Core query is recorded — no monkey-patching.

Usage::

    from sqlalchemy import create_engine
    import allstak
    from allstak.integrations.sqlalchemy import install as install_sqlalchemy

    allstak.init(api_key="...")
    engine = create_engine("postgresql://...")
    install_sqlalchemy(engine)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("allstak.sdk")


def install(engine: Any) -> None:
    """
    Attach AllStak database instrumentation to *engine*.

    Safe to call multiple times — listeners are only attached once per engine.
    No-op if SQLAlchemy is not installed.
    """
    try:
        from sqlalchemy import event
    except ImportError:
        return

    if getattr(engine, "_allstak_installed", False):
        return

    def _before_cursor_execute(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        context._allstak_start = time.time()

    def _after_cursor_execute(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        _record(statement, cursor, context, status="success", error=None, engine=engine)

    def _handle_error(ctx: Any) -> None:
        _record(
            getattr(ctx, "statement", "") or "",
            getattr(ctx, "cursor", None),
            getattr(ctx, "execution_context", None),
            status="error",
            error=str(getattr(ctx, "original_exception", "") or ctx)[:500],
            engine=engine,
        )

    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    event.listen(engine, "after_cursor_execute", _after_cursor_execute)
    event.listen(engine, "handle_error", _handle_error)

    engine._allstak_installed = True
    logger.debug("[AllStak] SQLAlchemy instrumentation installed on %s", engine)


def _record(statement: str, cursor: Any, ctx: Any, *, status: str, error: Optional[str], engine: Any) -> None:
    try:
        import allstak  # lazy — SDK may not be initialized

        client = allstak.get_client()
        if client is None:
            return

        start = getattr(ctx, "_allstak_start", None) if ctx is not None else None
        duration = (time.time() - start) * 1000 if start else 0.0

        from ..modules.database import normalize_query, detect_query_type

        norm = normalize_query(statement or "")
        rows = -1
        if cursor is not None:
            try:
                rc = getattr(cursor, "rowcount", -1)
                rows = int(rc) if rc is not None and rc >= 0 else -1
            except Exception:
                rows = -1

        db_type = ""
        db_name = ""
        try:
            db_type = engine.dialect.name or ""
            db_name = getattr(engine.url, "database", "") or ""
        except Exception:
            pass

        client.database.record(
            normalized_query=norm,
            duration_ms=duration,
            status=status,
            error_message=error,
            database_name=db_name,
            database_type=db_type,
            query_type=detect_query_type(norm),
            rows_affected=rows,
        )
    except Exception as exc:
        logger.debug("[AllStak] SQLAlchemy record failed: %s", exc)
