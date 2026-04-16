# AllStak Python SDK

Official Python SDK for [AllStak](https://allstak.dev) — error tracking,
structured logs, HTTP + DB monitoring, distributed tracing, and cron
monitoring with first-class FastAPI, Django, Flask, and SQLAlchemy support.

```bash
pip install allstak
```

## 60-second setup

```python
import allstak

allstak.init(
    api_key="ask_live_...",          # required
    environment="production",        # optional
    release="taskflow@1.4.2",        # optional
)

try:
    1 / 0
except Exception as e:
    allstak.capture_exception(e)
```

That's it. The first error will appear in your AllStak project within a few
seconds.

## FastAPI in two lines

```python
from fastapi import FastAPI
import allstak
from allstak.integrations.fastapi import AllStakFastAPI

allstak.init(api_key="ask_live_...")

app = FastAPI()
AllStakFastAPI(app, service="taskflow-api")
```

This automatically captures:

- every inbound request (method, path, status, duration, body size)
- every unhandled exception — with the full stack trace, request context,
  trace ID, and the authenticated user
- a fresh trace ID per request, linked to errors on the dashboard

If you want user context on errors, set it in your auth dependency:

```python
@app.get("/me")
def me(current_user = Depends(get_current_user)):
    allstak.set_user(user_id=str(current_user.id), email=current_user.email)
    return current_user
```

## Django

```python
# settings.py
MIDDLEWARE = [
    "allstak.integrations.django.AllStakMiddleware",
    # ...
]

ALLSTAK = {
    "api_key": "ask_live_...",
    "environment": "production",
}
```

## Flask

```python
from flask import Flask
from allstak.integrations.flask import AllStakFlask

app = Flask(__name__)
AllStakFlask(app)
```

## SQLAlchemy

One line hooks the event system — no monkey-patching — and works with
Postgres, MySQL, SQLite, MariaDB, and any other SQLAlchemy dialect:

```python
from sqlalchemy import create_engine
from allstak.integrations.sqlalchemy import install as install_sqlalchemy

engine = create_engine("postgresql://...")
install_sqlalchemy(engine)
```

Every ORM and Core query is captured with normalized SQL, timing, row count,
status, and error message. Queries are grouped by pattern in the dashboard.

## What gets captured automatically

Once `init()` has run (and, if applicable, a framework integration is
installed) the SDK captures:

| What                      | How                                        |
| ------------------------- | ------------------------------------------ |
| Python exceptions         | `allstak.capture_exception(e)` or framework middleware |
| Unhandled route exceptions| FastAPI / Django / Flask integrations      |
| Inbound HTTP requests     | FastAPI / Django / Flask integrations      |
| SQL queries               | `allstak.integrations.sqlalchemy.install`  |
| Log breadcrumbs           | Python `logging` (WARNING+) → auto         |
| `requests` lib breadcrumbs| auto-patched `requests.Session.send`       |
| User context              | `allstak.set_user(...)`                    |
| Trace context             | auto per request (framework integrations)  |

## Manual capture cheat sheet

```python
# Errors
allstak.capture_exception(e, metadata={"order_id": "ORD-123"})
allstak.capture_error(
    exception_class="StripeTimeout",
    message="Stripe /v1/charges timed out after 30s",
    level="error",
)

# Logs (buffered, flushed in the background)
allstak.log.info("Order placed", service="orders", metadata={"id": "ORD-1"})
allstak.log.warn("Slow query", service="db", metadata={"ms": 4500})
allstak.log.error("Payment failed", metadata={"gateway": "stripe"})
# valid levels: debug | info | warn | error | fatal  (NOT "warning")

# Outbound HTTP — with correct timing and status
with allstak.http.track_outbound("POST", "https://api.stripe.com/v1/charges") as call:
    resp = httpx.post("https://api.stripe.com/v1/charges", json=payload)
    call.set_response(resp.status_code, len(resp.content))

# Distributed tracing
with allstak.start_span("db.query", description="SELECT users") as span:
    span.set_tag("db.type", "postgresql")
    rows = db.execute(sql)

# Cron monitoring — slug auto-created on first ping
with allstak.cron.job("daily-report"):
    generate_report()
    # heartbeat automatically sent on exit (success | failed + message)

# User context
allstak.set_user(user_id="u-1", email="alice@example.com")
allstak.clear_user()

# Graceful shutdown (optional — atexit flush runs automatically)
allstak.flush()
```

## Dashboard mapping

| Your code                                  | Dashboard page        |
| ------------------------------------------ | --------------------- |
| `allstak.capture_exception`                | **Errors**, **Incidents** |
| `allstak.log.*`                            | **Logs**              |
| framework middleware (inbound)             | **Requests**          |
| `allstak.http.track_outbound`              | **Requests** (outbound) |
| `install_sqlalchemy(engine)`               | **Database**          |
| `allstak.start_span`                       | **Traces**            |
| `allstak.cron.job` / `allstak.cron.ping`   | **Cron Jobs**         |
| `allstak.set_user`                         | shown on Errors & Logs|

## Configuration

| Parameter          | Default                    | Notes |
| ------------------ | -------------------------- | ----- |
| `api_key`          | _required_                 | Your `ask_live_...` key. Never commit these. |
| `host`             | `http://localhost:8080`    | Override with your AllStak backend URL (self-hosted or SaaS). |
| `environment`      | `None`                     | e.g. `"production"`, `"staging"` |
| `release`          | `None`                     | e.g. `"taskflow@1.4.2"`. Shown on every error. |
| `flush_interval_ms`| `5000`                     | How often background buffers flush. |
| `buffer_size`      | `500`                      | Max buffered items per feature. Oldest dropped first. |
| `debug`            | `False`                    | Verbose SDK logging to stderr. |
| `auto_breadcrumbs` | `True`                     | Patch `requests` and `logging` for breadcrumbs. |

Environment variables: `ALLSTAK_API_KEY`, `ALLSTAK_HOST`,
`ALLSTAK_ENVIRONMENT`, `ALLSTAK_RELEASE`, `ALLSTAK_DEBUG`.

## Production notes

- **Never crashes your app.** The SDK swallows every exception from its own
  code paths. If ingestion fails (network, 4xx, 5xx exhausted), your request
  still completes.
- **Retries.** 5xx and network errors retry with exponential backoff
  (1s → 2s → 4s → 8s, +jitter, max 5 attempts). 4xx are not retried.
- **401 disables the SDK.** An invalid API key disables the SDK for the
  rest of the process — no further events are sent, a warning is logged once,
  and your app keeps running.
- **Flush on shutdown.** `atexit` triggers a best-effort flush (5s deadline).
- **Thread-safe.** All public APIs are safe to call from any thread.
- **No async I/O.** Uses synchronous `httpx` under the hood. Calls are
  non-blocking because buffers flush on a background thread.

## Troubleshooting

| Symptom                               | Fix                                              |
| ------------------------------------- | ------------------------------------------------ |
| No errors in dashboard                | Check `host` and `api_key`. Set `debug=True` to see outgoing requests. |
| 401 warning                           | Invalid API key. Create a new one in Settings → API Keys. |
| Inbound requests missing              | Make sure `AllStakFastAPI(app)` / `AllStakMiddleware` is registered. |
| DB queries missing                    | Call `install_sqlalchemy(engine)` on your engine. |
| Cron monitor not appearing            | It is auto-created on first ping; check the slug matches. |
| `warn` vs `warning`                   | For `allstak.log.*` use `warn`, not `warning`. |
| Events lost under burst               | Increase `buffer_size` or decrease `flush_interval_ms`. |

## Optional extras

```bash
pip install "allstak[fastapi]"     # starlette
pip install "allstak[django]"      # django
pip install "allstak[flask]"       # flask
pip install "allstak[sqlalchemy]"  # sqlalchemy
pip install "allstak[all]"         # everything
```

## Full FastAPI example

```python
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

import allstak
from allstak.integrations.fastapi import AllStakFastAPI
from allstak.integrations.sqlalchemy import install as install_sqlalchemy

engine = create_engine("sqlite:///./app.db")
SessionLocal = sessionmaker(bind=engine)

allstak.init(
    api_key="ask_live_...",
    environment="production",
    release="taskflow@1.4.2",
)
install_sqlalchemy(engine)

app = FastAPI()
AllStakFastAPI(app, service="taskflow-api")


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.post("/orders/{order_id}/charge")
def charge(order_id: int, db: Session = Depends(get_db)):
    allstak.log.info("charging", service="billing", metadata={"orderId": order_id})

    with allstak.start_span("db.load-order") as span:
        order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).first()
        span.set_tag("order.id", str(order_id))

    if order is None:
        raise HTTPException(404, "order not found")

    # Outbound charge
    with allstak.http.track_outbound("POST", "https://api.stripe.com/v1/charges") as call:
        resp = httpx.post("https://api.stripe.com/v1/charges", json={"amount": order["total"]})
        call.set_response(resp.status_code, len(resp.content))
        if resp.status_code != 200:
            raise HTTPException(502, "stripe failed")

    return {"ok": True}
```

## License

MIT
