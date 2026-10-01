"""Functional tests — run by the pipeline against a deployed environment.

Deliberately thin for now. The pipeline runs `pytest tests/functional` with
`junit(allowEmptyResults: true)`, so an empty suite would pass silently; this
keeps the directory honest and gives the real functional cases somewhere to
land. Anything needing an authenticated judicial user belongs here rather than
in tests/unit.
"""

from __future__ import annotations

import os

import httpx

TEST_URL = os.environ.get("TEST_URL", "http://localhost:8000").rstrip("/")


def test_unauthenticated_request_is_rejected() -> None:
    """A protected route must not serve data without a bearer token."""
    response = httpx.get(f"{TEST_URL}/api/v1/jobs", timeout=30.0)
    assert response.status_code in (401, 403), (
        f"expected an auth challenge from /api/v1/jobs, got {response.status_code}"
    )
