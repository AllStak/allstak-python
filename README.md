# allstak

**Error tracking, logs, and request tracing for Python — works out of the box with Django, Flask, and FastAPI.**

[![PyPI version](https://img.shields.io/pypi/v/allstak.svg)](https://pypi.org/project/allstak/)
[![CI](https://github.com/allstak-io/allstak-python/actions/workflows/ci.yml/badge.svg)](https://github.com/allstak-io/allstak-python/actions)
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

## Installation

```bash
pip install allstak
```

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

## Fail-Open Reliability

AllStak telemetry is best-effort. Runtime capture APIs enqueue into bounded
background workers and drop telemetry before harming the host process. If
AllStak ingest is down, slow, rate-limiting, under maintenance, or unreachable,
your application should keep serving traffic normally.

- Capture APIs swallow SDK transport failures internally.
- Error, log, HTTP, replay, trace, and database buffers are bounded.
- DNS, connection, timeout, 429, 500, and 503 failure modes are covered by
  automated fail-open tests.
- Django, Flask, and FastAPI middleware catch SDK failures and return the
  customer response unchanged.
- Shutdown is bounded; runtime shutdown paths do not drain through a slow
  AllStak endpoint.

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
- Source: https://github.com/allstak-io/allstak-python

## License

MIT © AllStak
