# allstak

**Error tracking, logs, and request tracing for Python — works out of the box with Django, Flask, and FastAPI.**

[![PyPI version](https://img.shields.io/pypi/v/allstak.svg)](https://pypi.org/project/allstak/)
[![CI](https://github.com/AllStak/allstak-python/actions/workflows/ci.yml/badge.svg)](https://github.com/AllStak/allstak-python/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Official AllStak SDK for Python — captures exceptions, structured logs, HTTP requests, database queries, distributed traces, cron heartbeats, and session replay for Django, Flask, FastAPI, and plain Python services.

## Dashboard

View captured events live at [app.allstak.sa](https://app.allstak.sa).

![AllStak dashboard](https://app.allstak.sa/images/dashboard-preview.png)

## Features

- Exception and `sys.excepthook` capture
- Structured logs with a `logging` handler bridge
- `requests` auto-instrumentation for breadcrumbs and outbound HTTP telemetry
- Django / Flask / FastAPI middleware for inbound request capture
- Distributed tracing with context-managed spans
- Cron heartbeats via `allstak.cron.job` context manager
- Configurable via `AllStakConfig.from_env()` for 12-factor apps

## What You Get

Once integrated, every event flows to your AllStak dashboard:

- **Errors** — stack traces, breadcrumbs, release + environment tags
- **Logs** — structured logs bridged from `logging` with search and filters
- **HTTP** — inbound and outbound request timing, status codes, failed calls
- **Performance** — slow endpoints and DB queries
- **Cron monitors** — scheduled job success/failure tracking
- **Alerts** — email and webhook notifications on regressions

## Installation

> **Not yet on PyPI.** `pip install allstak` is reserved but does not
> resolve a published artifact yet. Until first publish lands (tracked
> in [`docs/devops/sdk-python-dotnet-first-publish.md`](https://github.com/AllStak/allstak/blob/dev/docs/devops/sdk-python-dotnet-first-publish.md)
> in the platform monorepo), install directly from source:
>
> ```bash
> pip install "git+https://github.com/AllStak/allstak-python.git@main"
> ```
>
> Once `0.1.x` ships on PyPI, the canonical install becomes:
>
> ```bash
> pip install allstak
> ```

## Quick Start

> Create a project at [app.allstak.sa](https://app.allstak.sa) to get your API key.

```python
import os
import allstak

allstak.init(
    api_key=os.getenv("ALLSTAK_API_KEY"),
    environment="production",
    release="myapp@1.0.0",
)

allstak.capture_exception(Exception("test: hello from allstak-python"))
```

Run the file — the test error appears in your dashboard within seconds.

## Get Your API Key

1. Sign up at [app.allstak.sa](https://app.allstak.sa)
2. Create a project
3. Copy your API key from **Project Settings → API Keys**
4. Export it as `ALLSTAK_API_KEY` or pass it to `allstak.init(...)`

## Configuration

| Option | Type | Required | Default | Description |
|---|---|---|---|---|
| `api_key` | `str` | yes | — | Project API key (`ask_live_…`) |
| `host` | `str` | no | `https://api.allstak.sa` | Ingest host override |
| `environment` | `str` | no | — | Deployment env (`production`, `staging`) |
| `release` | `str` | no | — | App version or release tag |
| `debug` | `bool` | no | `False` | Verbose SDK logging to stderr |
| `flush_interval_ms` | `int` | no | `5000` | Background flush cadence |
| `buffer_size` | `int` | no | `500` | Max items per buffer |
| `auto_breadcrumbs` | `bool` | no | `True` | Auto-instrument `requests` and `logging` |
| `max_breadcrumbs` | `int` | no | `50` | Breadcrumb ring buffer size |

Environment variables: `ALLSTAK_API_KEY`, `ALLSTAK_HOST`, `ALLSTAK_ENVIRONMENT`, `ALLSTAK_RELEASE`, `ALLSTAK_DEBUG`.

## Example Usage

Capture an exception with metadata:

```python
try:
    charge_card(order)
except PaymentError as e:
    allstak.capture_exception(e, metadata={"order_id": order.id})
```

Send a structured log:

```python
allstak.log.info("User signed up", metadata={"user_id": user.id})
```

Set user context and tags:

```python
allstak.set_user(id=user.id, email=user.email)
allstak.set_tag("region", "eu-west-1")
```

Report a cron run:

```python
with allstak.cron.job("daily-report"):
    generate_report()
```

## Production Endpoint

Production endpoint: `https://api.allstak.sa`. Override via `host` (or `ALLSTAK_HOST`) for self-hosted deployments:

```python
allstak.init(api_key=os.getenv("ALLSTAK_API_KEY"), host="https://allstak.mycorp.com")
```

## Links

- Documentation: https://docs.allstak.sa
- Dashboard: https://app.allstak.sa
- Source: https://github.com/AllStak/allstak-python

## License

MIT © AllStak
