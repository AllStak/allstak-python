# Changelog

All notable changes to the AllStak Python SDK.
This project follows [Semantic Versioning](https://semver.org/).

## 0.2.0 — 2026-04-11

First production-ready release after a full real-world validation pass against a
FastAPI + SQLAlchemy + SQLite application driving real authentication, CRUD,
logs, outbound HTTP, cron jobs, and real exceptions — all verified end-to-end
in the AllStak dashboard.

### Highlights

- **First-class FastAPI / Starlette integration** (`allstak.integrations.fastapi`)
  — a single line (`AllStakFastAPI(app)`) captures inbound HTTP requests,
  per-request trace IDs, unhandled exceptions, request context (method / path /
  host / status / user-agent), and links every captured error to the owning
  trace.
- **SQLAlchemy integration** (`allstak.integrations.sqlalchemy.install`) —
  hooks into SQLAlchemy's `before_cursor_execute` / `after_cursor_execute` /
  `handle_error` events to record every ORM and Core query with real
  timings, row counts, dialect detection, and error status. No monkey-patching.
- **`RequestContext`** is now a first-class part of the error payload so the
  dashboard can show the exact HTTP method / path / host / status on every
  captured exception.
- **`trace_id` is attached to errors automatically** (not just via metadata),
  so the "Linked Traces" panel in the dashboard works out of the box.

### Added

- `allstak/integrations/fastapi.py` — FastAPI / Starlette ASGI middleware and
  convenience wrapper, plus a new `fastapi` optional-dependency group.
- `allstak/integrations/sqlalchemy.py` — SQLAlchemy event-based DB
  instrumentation with a `sqlalchemy` optional-dependency group.
- `allstak.RequestContext` — new dataclass exported at the package root.
- `allstak.http.track_outbound(...)` now yields a recorder object so callers can
  attach the real response status and body size (`call.set_response(status, size)`).
- Extensive production-grade PyPI classifiers, URLs (homepage, docs, repo,
  issues, changelog), and an `all` extra that installs every optional
  integration.

### Changed

- **SDK version bumped from `0.1.0` → `0.2.0`.** New features + new public
  API surface (`RequestContext`, FastAPI / SQLAlchemy integrations,
  `track_outbound` recorder) justify a minor bump under semver: no breaking
  changes for existing code, but meaningful new functionality.
- `allstak.cron.job(...)` is now a **safe no-op context manager** when the SDK
  is not initialized — previously it returned a plain function and broke
  the documented `with allstak.cron.job("slug"):` idiom for uninitialized apps.
- `sqlite3` auto-instrumentation now uses a `Connection` factory subclass
  instead of trying to reassign `sqlite3.Cursor.execute` (which raises `TypeError`
  on a built-in type). Standalone `sqlite3` usage is now actually captured.
- The FastAPI middleware populates `scope["state"]` if missing so auth
  dependencies can safely set `request.state.user` for error correlation.
- `examples/basic_usage.py` now reads `ALLSTAK_API_KEY` from the environment
  and aborts cleanly if it is missing, instead of shipping a hard-coded key.

### Fixed

- `HttpMonitorModule.track_outbound` no longer records every outbound call as
  `status_code=0`. The context manager now yields a recorder that carries the
  real status and size, and automatically tags the request with the exception
  class name on failure.
- Unhandled exceptions from FastAPI route handlers are now captured
  automatically with full request context, stack trace, trace ID, and user
  context — previously you had to call `allstak.capture_exception` manually.
- Error events now include the current `trace_id` as a proper top-level field,
  so the dashboard's Linked Traces panel resolves immediately.

### What's new for Python users

- If you use FastAPI:
  ```python
  from fastapi import FastAPI
  import allstak
  from allstak.integrations.fastapi import AllStakFastAPI

  allstak.init(api_key="ask_live_...")
  app = FastAPI()
  AllStakFastAPI(app, service="my-api")
  ```
  That is the whole setup. Every request, every exception, and every trace are
  captured automatically.
- If you use SQLAlchemy (with any dialect — Postgres, MySQL, SQLite, etc.):
  ```python
  from sqlalchemy import create_engine
  from allstak.integrations.sqlalchemy import install as install_sqlalchemy

  engine = create_engine("postgresql://...")
  install_sqlalchemy(engine)
  ```
- Cron monitoring still works exactly as before, but you can now safely use
  `with allstak.cron.job("slug"):` even from scripts that might run without
  the SDK initialized (tests, dry-runs, etc.).

### Breaking changes

None. `0.1.0` code keeps working as-is.

---

## 0.1.0 — 2026-03-28

Initial beta release. Error capture, logs, HTTP monitoring, distributed
tracing, cron heartbeats, basic Django & Flask integrations, and transport
with retry/backoff + 401 disable.
