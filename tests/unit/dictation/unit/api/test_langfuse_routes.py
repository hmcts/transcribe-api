"""The Langfuse feedback proxy: events attach only to a trace Langfuse v3+ can address."""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from transcribe_api.domain.models_dictation import User

ROUTES = "transcribe_api.api.routes_dictation"
OTEL_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
LEGACY_TRACE_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


@pytest.fixture
def user() -> User:
    user = User(id=uuid.uuid4(), email="judge@justice.gov.uk", azure_user_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890")
    user.__dict__["app_roles"] = ["Judge"]
    return user


@pytest.fixture
def client(user):
    import transcribe_api.api.routes_dictation
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user

    app = FastAPI()
    app.include_router(transcribe_api.api.routes_dictation.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_event_is_attached_to_the_requested_trace(client, user, mocker) -> None:
    event = mocker.patch(f"{ROUTES}.langfuse_client.create_event")
    body = {"trace_id": OTEL_TRACE_ID, "name": "copied", "metadata": {"button": "copy"}}

    assert client.post("/api/langfuse/trace", json=body).status_code == 200

    kwargs = event.call_args.kwargs
    assert kwargs["trace_context"] == {"trace_id": OTEL_TRACE_ID}
    assert kwargs["metadata"] == {"button": "copy", "user_id": str(user.id)}


def test_event_for_a_legacy_trace_is_rejected(client, mocker) -> None:
    event = mocker.patch(f"{ROUTES}.langfuse_client.create_event")

    response = client.post("/api/langfuse/trace", json={"trace_id": LEGACY_TRACE_ID, "name": "copied"})

    assert response.status_code == 400
    event.assert_not_called()


@pytest.mark.parametrize("trace_id", [OTEL_TRACE_ID, LEGACY_TRACE_ID])
def test_score_goes_to_the_requested_trace(client, mocker, trace_id) -> None:
    score = mocker.patch(f"{ROUTES}.langfuse_client.create_score")
    body = {"trace_id": trace_id, "name": "rating", "value": 4, "comment": "good"}

    assert client.post("/api/langfuse/score", json=body).status_code == 200

    score.assert_called_once_with(trace_id=trace_id, name="rating", value=4.0, comment="good")
