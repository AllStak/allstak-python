"""Shared test fixtures and configuration."""

import os
import pytest

# Real backend settings — used for integration tests
REAL_API_KEY = os.environ.get("ALLSTAK_API_KEY", "ask_live_o5fmoedqr14vxm47rltn9frjpazjszh7")
REAL_HOST = os.environ.get("ALLSTAK_HOST", "http://localhost:8080")


@pytest.fixture
def real_api_key() -> str:
    return REAL_API_KEY


@pytest.fixture
def real_host() -> str:
    return REAL_HOST
