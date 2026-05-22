# allstak

AllStak SDK for Python, Django, Flask, FastAPI, and plain services. Captures exceptions, logs, inbound and outbound HTTP requests, spans, database telemetry, and cron heartbeats.

## Install

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

## Setup

```python
import os
import allstak

allstak.init(
    api_key=os.getenv("ALLSTAK_API_KEY"),
    environment=os.getenv("APP_ENV", "production"),
    release=os.getenv("ALLSTAK_RELEASE"),
)

allstak.log.info("worker started")
allstak.capture_exception(RuntimeError("checkout failed"))
```

## FastAPI

```python
from fastapi import FastAPI
from allstak.integrations.fastapi import AllStakFastAPI

app = FastAPI()
AllStakFastAPI(app, service="checkout-api")
```

## Flask

```python
from flask import Flask
from allstak.integrations.flask import AllStakFlask

app = Flask(__name__)
AllStakFlask(app)
```

## Django

Add the middleware:

```python
MIDDLEWARE = [
    "allstak.integrations.django.AllStakDjangoMiddleware",
    *MIDDLEWARE,
]
```

## Spans

```python
with allstak.start_span("checkout.authorize", tags={"provider": "payments"}):
    authorize_payment()
```

## Configuration

| Option | Description |
| --- | --- |
| `api_key` | Project API key. |
| `host` | Optional ingest host override for self-hosted AllStak. |
| `environment` | Deployment environment. |
| `release` | App version or commit SHA. |
| `flush_interval_ms` | Background flush interval. |
| `buffer_size` | Max buffered events. |

## Privacy

The SDK redacts common sensitive headers and fields. Avoid putting secrets in custom metadata.

## Troubleshooting

- No events: confirm `ALLSTAK_API_KEY` is set before `allstak.init(...)`.
- Missing request telemetry: register the framework integration during app startup.
- Short-lived script: call `allstak.get_client().flush()` before exit when a client is initialized.

## License

MIT
