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
        try:
            import allstak
            from ..modules.database import detect_query_type, normalize_query

            client = allstak.get_client()
            if client is not None:
                normalized = normalize_query(statement or "")
                db_type = ""
                try:
                    db_type = engine.dialect.name or ""
                except Exception:
                    pass
                context._allstak_span = client.start_span(
                    "db.query",
                    description=normalized[:300],
                    tags={
                        "db.system": db_type,
                        "db.operation": detect_query_type(normalized),
                    },
                )
        except Exception:
            context._allstak_span = None

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

        span = getattr(ctx, "_allstak_span", None) if ctx is not None else None
        span_context = {}
        if span is not None:
            try:
                span_context = {
                    "trace_id": getattr(span, "trace_id", "") or "",
                    "span_id": getattr(span, "span_id", "") or "",
                    "parent_span_id": getattr(span, "parent_span_id", "") or "",
                }
                span.set_tag("db.system", db_type)
                span.set_tag("db.name", db_name)
                span.finish("error" if status == "error" else "ok")
            except Exception:
                span_context = {}

        client.database.record(
            normalized_query=norm,
            duration_ms=duration,
            status=status,
            error_message=error,
            database_name=db_name,
            database_type=db_type,
            query_type=detect_query_type(norm),
            rows_affected=rows,
            **span_context,
        )
    except Exception as exc:
        logger.debug("[AllStak] SQLAlchemy record failed: %s", exc)
