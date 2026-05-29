"""Shared test fixtures and configuration."""

import logging
import os
import pytest

# Real backend settings — used only when integration tests are explicitly enabled.
REAL_API_KEY = os.environ.get("ALLSTAK_API_KEY", "")
REAL_HOST = os.environ.get("ALLSTAK_HOST", "http://localhost:8080")


@pytest.fixture(autouse=True)
def _isolate_global_instrumentation():
    """Undo the process-global side effects of ``allstak.init()`` after each test.

    ``Client.init()`` (with the default ``auto_breadcrumbs=True``) monkeypatches
    ``requests.Session.send`` with a breadcrumb wrapper and attaches a handler to
    the root logger. Those mutations live on global objects, so any suite that
    calls ``init()`` and resets only ``allstak_client._client`` /
    ``_initialized_once`` still leaks the instrumentation into every later test.

    This was masked purely by alphabetical collection order (``test_auto_breadcrumbs``
    ran before any ``init()``-calling suite); under any other order the idempotency
    test in ``test_auto_breadcrumbs`` saw an already-patched ``Session.send`` and
    failed. Snapshot the global state here and restore it on teardown so the
    instrumentation cannot leak across suites regardless of ordering.
    """
    try:
        import requests  # type: ignore[import-untyped]
        original_send = requests.Session.send
    except Exception:  # pragma: no cover — requests always present in test env
        requests = None
        original_send = None

    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)

    yield

    if requests is not None:
        requests.Session.send = original_send

    # Drop any breadcrumb logging handlers added during the test.
    for handler in list(root_logger.handlers):
        if handler not in original_handlers:
            root_logger.removeHandler(handler)


def pytest_collection_modifyitems(config, items):
    if os.environ.get("ALLSTAK_RUN_INTEGRATION") == "1":
        return

    skip_integration = pytest.mark.skip(
        reason="set ALLSTAK_RUN_INTEGRATION=1 and ALLSTAK_API_KEY to run live integration tests"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


@pytest.fixture
def real_api_key() -> str:
    return REAL_API_KEY


@pytest.fixture
def real_host() -> str:
    return REAL_HOST
