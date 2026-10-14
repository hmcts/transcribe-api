"""The CNP pipeline's startup check polls GET /health at the root."""

from fastapi.testclient import TestClient

from transcribe_api.api.app import create_app


def test_root_health_is_served_unauthenticated() -> None:
    response = TestClient(create_app()).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
