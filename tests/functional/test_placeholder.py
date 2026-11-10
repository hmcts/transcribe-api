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
import pytest

TEST_URL = os.environ.get("TEST_URL", "http://localhost:8000").rstrip("/")
# Jenkinsfile_CNP sets false for PR previews, where token-less requests are the
# preview user (AUTH_PREVIEW_USER_ENABLED).
AUTH_EXPECTED = os.environ.get("AUTH_EXPECTED", "true").lower() != "false"

PREVIEW_USER_EMAIL = "preview-user@preview.platform.hmcts.net"


@pytest.mark.skipif(not AUTH_EXPECTED, reason="previews authenticate token-less requests as the preview user")
def test_unauthenticated_request_is_rejected() -> None:
    """A protected route must not serve data without a bearer token."""
    response = httpx.get(f"{TEST_URL}/api/v1/jobs", timeout=30.0)
    assert response.status_code in (401, 403), (
        f"expected an auth challenge from /api/v1/jobs, got {response.status_code}"
    )


@pytest.mark.skipif(AUTH_EXPECTED, reason="only PR previews have the preview user")
def test_preview_serves_token_less_requests_as_the_preview_user() -> None:
    jobs = httpx.get(f"{TEST_URL}/api/v1/jobs", timeout=30.0)
    assert jobs.status_code == 200, f"/api/v1/jobs returned {jobs.status_code}"
    me = httpx.get(f"{TEST_URL}/api/users/me", timeout=30.0)
    assert me.status_code == 200, f"/api/users/me returned {me.status_code}"
    assert me.json()["email"] == PREVIEW_USER_EMAIL


@pytest.mark.skipif(AUTH_EXPECTED, reason="only PR previews have the preview user")
def test_preview_still_rejects_an_invalid_token() -> None:
    response = httpx.get(f"{TEST_URL}/api/v1/jobs", headers={"Authorization": "Bearer not-a-real-token"}, timeout=30.0)
    assert response.status_code == 401, f"expected 401 for an invalid token, got {response.status_code}"
