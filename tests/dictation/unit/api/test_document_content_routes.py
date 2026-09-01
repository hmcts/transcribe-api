import pytest

pytest.skip("requires database env vars not available in CI unit tests", allow_module_level=True)

from transcribe_api.domain.auth.dependencies_dictation import get_allowlisted_user  # noqa: E402


async def _fake_allowlisted_user() -> None:
    return None


def test_get_document_content_route_returns_allowlisted_content(sync_test_client) -> None:
    from main import app

    app.dependency_overrides[get_allowlisted_user] = _fake_allowlisted_user

    try:
        response = sync_test_client.get("/document-content")
    finally:
        app.dependency_overrides.pop(get_allowlisted_user, None)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"

    body = response.json()
    assert body["hearing_types"][0]["id"] == "face_to_face"
    assert any(item["id"] == "eea_deportation" for item in body["legal_frameworks"])
    assert [status["id"] for status in body["anonymity"]["statuses"]] == [
        "granted",
        "sought_but_refused",
        "not_sought",
    ]
