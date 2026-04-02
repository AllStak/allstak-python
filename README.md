# AllStak Python SDK

Official Python SDK for [AllStak](https://allstak.dev) — production-grade observability, error tracking, structured logging, HTTP monitoring, session replay, cron job monitoring, and feature flags.

## Installation

```bash
pip install allstak
```

For development (from source):
```bash
git clone ...
cd allstak-python
pip install -e ".[dev]"
```

## Quick Start

```python
import allstak

allstak.init(
    api_key="ask_live_...",
    host="https://your-allstak-instance.com",
    environment="production",
    release="v1.0.0",
)

# Capture exceptions
try:
    risky_operation()
except Exception as e:
    allstak.capture_exception(e)

# Logs
allstak.log.info("User signed up", service="auth", metadata={"plan": "pro"})

# Flush before shutdown
allstak.flush()
```

## Configuration

| Parameter | Type | Default | Description |
|---|---|---|---|
| `api_key` | str | required | Raw API key (`X-AllStak-Key`) |
| `host` | str | `http://localhost:8080` | AllStak backend URL |
| `environment` | str | None | e.g. `"production"` |
| `release` | str | None | App version e.g. `"v1.4.2"` |
| `flush_interval_ms` | int | 5000 | Background flush interval |
| `buffer_size` | int | 500 | Max buffered items per feature |
| `debug` | bool | False | Verbose SDK logging to stderr |

Environment variables: `ALLSTAK_API_KEY`, `ALLSTAK_HOST`, `ALLSTAK_ENVIRONMENT`, `ALLSTAK_RELEASE`.

## Features

### Error Tracking

```python
# Capture a Python exception
try:
    1 / 0
except ZeroDivisionError as e:
    allstak.capture_exception(
        e,
        level="error",
        metadata={"user_id": "usr-001"},
    )

# Capture without an exception object
allstak.capture_error(
    exception_class="ExternalServiceError",
    message="Stripe API returned 503",
    level="error",
)

# Set user context
allstak.set_user(user_id="usr-001", email="user@example.com")
```

### Logs

```python
allstak.log.debug("Cache miss", service="cache")
allstak.log.info("Order placed", metadata={"order_id": "ORD-1234"})
allstak.log.warn("Slow query", service="db", metadata={"ms": 4500})
allstak.log.error("Payment failed", metadata={"gateway": "stripe"})
allstak.log.fatal("Out of memory")
```

Valid log levels: `debug`, `info`, `warn`, `error`, `fatal`.
> Note: use `warn` not `warning` for logs.

### HTTP Monitoring

```python
import time

start = time.monotonic()
response = requests.get("https://api.example.com/data")
duration = int((time.monotonic() - start) * 1000)

allstak.http.record(
    direction="outbound",       # or "inbound"
    method="GET",
    host="api.example.com",
    path="/data",               # query params stripped automatically
    status_code=response.status_code,
    duration_ms=duration,
    request_size=0,
    response_size=len(response.content),
)
```

### Session Replay

```python
import uuid

with allstak.replay.start_session() as session:
    session.record("navigation", {"from": "/home", "to": "/checkout"})
    session.record("api_call", {"endpoint": "/api/orders", "status": 200})
    # session.end() called automatically on exit
```

### Cron Job Monitoring

```python
# Context manager (recommended)
with allstak.cron.job("daily-report") as job:
    generate_report()
    # heartbeat sent automatically with status="success"
    # on exception: status="failed", exception re-raised

# Manual
handle = allstak.cron.start("payment-reconciliation")
try:
    reconcile()
    allstak.cron.finish(handle, "success", message="Processed 5000 records")
except Exception as e:
    allstak.cron.finish(handle, "failed", message=str(e))
    raise
```

> The cron monitor slug must be created in the AllStak management console first.

### Feature Flags

> Feature flags require an OAuth2 Bearer JWT token (management API).
> Not available for ingestion-only API keys.

```python
from allstak.client import AllStakClient
from allstak.config import AllStakConfig
from allstak.modules.flags import FeatureFlagModule

flags = FeatureFlagModule(
    config=AllStakConfig(api_key="...", host="..."),
    bearer_token="eyJhbGci...",
    project_id="uuid-here",
)

result = flags.get("new-checkout-flow", user_id="usr-001")
if result.enabled:
    use_new_checkout()
```

## Framework Integrations

### Django

```python
# settings.py
MIDDLEWARE = [
    "allstak.integrations.django.AllStakMiddleware",
    # ...
]

ALLSTAK = {
    "api_key": "ask_live_...",
    "host": "http://localhost:8080",
    "environment": "production",
}
```

### Flask

```python
from flask import Flask
from allstak.integrations.flask import AllStakFlask

app = Flask(__name__)
AllStakFlask(app)
```

## SDK Behavior Contract

| Rule | Value |
|---|---|
| Connection timeout | 3 seconds |
| Read timeout | 3 seconds |
| Retry strategy | Exponential backoff: 1s → 2s → 4s → 8s |
| Max retries | 5 |
| Buffer size (per feature) | 500 items |
| Flush interval | 5 seconds |
| On 401 | Disable SDK, emit warning, no retry |
| On 5xx | Retry with backoff |
| On 400/422 | No retry (client error) |
| Fail-safe | Never raise from SDK code |

## Limitations

- Session replay in Python captures server-side events only (no DOM capture — that's browser-only)
- Feature flags require a management JWT, not an ingestion API key
- Cron heartbeat requires the slug to be pre-registered in the AllStak console
- No async/await support in this version (synchronous httpx is used)

## License

MIT
