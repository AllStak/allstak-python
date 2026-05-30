"""Shared test fixtures and configuration."""

import importlib.util
import logging
import os
import pytest

# Real backend settings — used only when integration tests are explicitly enabled.
REAL_API_KEY = os.environ.get("ALLSTAK_API_KEY", "")
REAL_HOST = os.environ.get("ALLSTAK_HOST", "http://localhost:8080")

# Test modules that import an optional framework at module top level. When the
# framework is not installed (e.g. an interpreter where the optional extra does
# not resolve), skip the whole module at collection time instead of erroring out
# with a ModuleNotFoundError during import. The framework integrations themselves
# remain optional, so their tests are too.
_FRAMEWORK_TEST_MODULES = {
    "test_django_integration.py": "django",
    "test_django_appconfig.py": "django",
    "test_fastapi_integration.py": "fastapi",
    "test_fastapi_autoinstrument.py": "fastapi",
}

collect_ignore = [
    test_module
    for test_module, required_pkg in _FRAMEWORK_TEST_MODULES.items()
    if importlib.util.find_spec(required_pkg) is None
]


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

    # ``allstak.init`` also patches Starlette's ASGI entry point (the
    # FastAPI/Starlette autoinstrument shim). Snapshot it so the global patch
    # cannot leak across suites regardless of collection order.
    try:
        from starlette.applications import Starlette  # type: ignore[import-untyped]
        original_call = Starlette.__call__
    except Exception:  # pragma: no cover — starlette present in test env
        Starlette = None
        original_call = None

    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)

    yield

    if requests is not None:
        requests.Session.send = original_send

    if Starlette is not None and original_call is not None:
        Starlette.__call__ = original_call

    # Drop any breadcrumb / logging-bridge handlers added during the test.
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
