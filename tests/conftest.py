"""Shared test fixtures and configuration."""

import os
import pytest

# Real backend settings — used only when integration tests are explicitly enabled.
REAL_API_KEY = os.environ.get("ALLSTAK_API_KEY", "")
REAL_HOST = os.environ.get("ALLSTAK_HOST", "http://localhost:8080")


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
