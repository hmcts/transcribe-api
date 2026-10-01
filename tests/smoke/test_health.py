"""Smoke tests — run by the pipeline against a freshly deployed environment.

The common pipeline invokes `pytest tests/smoke` with
`junit(allowEmptyResults: false)`, so this package must exist and must produce
at least one result or the build fails on an empty report rather than on
anything meaningful.

TEST_URL is set by the pipeline's testEnv wrapper to the environment that was
just deployed.
"""

from __future__ import annotations

import os

import httpx
import pytest

TEST_URL = os.environ.get("TEST_URL", "http://localhost:8000").rstrip("/")
TIMEOUT = httpx.Timeout(30.0)


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=TEST_URL, timeout=TIMEOUT, follow_redirects=True) as c:
        yield c


def test_dictation_health(client: httpx.Client) -> None:
    """The dictation surface is mounted at /api (see api/app.py)."""
    response = client.get("/api/health")
    assert response.status_code == 200, f"{TEST_URL}/api/health returned {response.status_code}"
    assert response.json()["status"] == "ok"


def test_recording_health(client: httpx.Client) -> None:
    """The recording router carries its own /api/v1 prefix, so it is a separate surface."""
    response = client.get("/api/v1/health")
    assert response.status_code == 200, f"{TEST_URL}/api/v1/health returned {response.status_code}"
    assert response.json()["status"] == "ok"


def test_openapi_served(client: httpx.Client) -> None:
    """Proves the app assembled both routers rather than merely starting."""
    response = client.get("/api/openapi.json")
    assert response.status_code == 200
    paths = response.json().get("paths", {})
    assert any(p.startswith("/api/v1/") for p in paths), "recording routes missing from the running app"
    assert any(p.startswith("/api/") and not p.startswith("/api/v1/") for p in paths), (
        "dictation routes missing from the running app"
    )
