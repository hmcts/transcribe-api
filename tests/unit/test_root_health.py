"""The CNP pipeline's startup checker calls GET <AKS_TEST_URL>/health — a fixed
path in cnp-jenkins-library (vars/helmInstall.groovy) — before it runs any
tests. It accepts any status as "reachable" but retries until it sees 200, so
the app must answer at the root, not only under /api.
"""

from fastapi.testclient import TestClient

from transcribe_api.api.app import create_app


def test_root_health_is_served_unauthenticated() -> None:
    response = TestClient(create_app()).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
