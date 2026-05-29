# allstak

[![PyPI](https://img.shields.io/pypi/v/allstak.svg)](https://pypi.org/project/allstak/)
[![Python versions](https://img.shields.io/pypi/pyversions/allstak.svg)](https://pypi.org/project/allstak/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/AllStak/allstak-python/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/AllStak/allstak-python/actions/workflows/ci.yml)

AllStak SDK for Python, Django, Flask, FastAPI, and plain services. Captures exceptions, logs, inbound and outbound HTTP requests, spans, database telemetry, and cron heartbeats.

## Install

```bash
pip install allstak
```

Framework extras are opt-in so the base install stays minimal:

```bash
pip install "allstak[fastapi]"     # FastAPI/Starlette helpers
pip install "allstak[flask]"       # Flask helpers
pip install "allstak[django]"      # Django middleware + integrations
pip install "allstak[sqlalchemy]"  # SQLAlchemy query telemetry
pip install "allstak[all]"         # everything above
```

Requires Python 3.9+. Wheels are universal (`py3-none-any`). Every release is
built reproducibly under a pinned `SOURCE_DATE_EPOCH`, validated via
`twine check --strict`, smoke-installed on Python 3.10/3.11/3.12, and
uploaded via PyPI Trusted Publishers (OIDC) with PEP 740 Sigstore
attestations.

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

## Contributing and Support

- Report bugs with the GitHub bug report template: https://github.com/AllStak/allstak-python/issues/new/choose
- Open pull requests using the checklist in [CONTRIBUTING.md](CONTRIBUTING.md).
- Report security vulnerabilities privately through [SECURITY.md](SECURITY.md).

## License

MIT
