"""X-Request-Id handling (hmcts-otel RequestIdMiddleware) with this app's exception handlers."""

import pytest
from fastapi import FastAPI
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.testclient import TestClient
from hmcts_otel import get_request_id
from hmcts_otel.fastapi import RequestIdMiddleware, request_id_for

from transcribe_api.runtime.exception_handlers import http_exception_handler

UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"


@pytest.fixture
def client():
    app = FastAPI()
    seen = {}

    @app.get("/ok")
    def ok():
        seen["rid"] = get_request_id()
        return {"ok": True}

    @app.get("/missing")
    def missing():
        raise FastAPIHTTPException(status_code=404, detail="Not found")

    @app.exception_handler(FastAPIHTTPException)
    async def handle(request, exc):
        return await http_exception_handler(exc, request_id_for(request))

    app.add_middleware(RequestIdMiddleware)
    test_client = TestClient(app)
    test_client.seen = seen
    return test_client


def test_generates_request_id_when_absent(client):
    response = client.get("/ok")
    rid = response.headers["X-Request-Id"]
    assert len(rid) == 32
    assert client.seen["rid"] == rid


def test_uses_canonical_request_id_from_client(client):
    response = client.get("/ok", headers={"X-Request-Id": UUID})
    assert response.headers["X-Request-Id"] == UUID
    assert client.seen["rid"] == UUID


@pytest.mark.parametrize(
    "supplied",
    ["Jane.Smith.AB12345", "PA/50123/2024", "07700900123", "12345678901234567890", "ABCDE12345", "x" * 100],
)
def test_free_form_or_numeric_request_ids_are_replaced(client, supplied):
    response = client.get("/ok", headers={"X-Request-Id": supplied})
    assert response.headers["X-Request-Id"] != supplied
    assert client.seen["rid"] == response.headers["X-Request-Id"]


def test_error_body_carries_the_request_id(client):
    response = client.get("/missing", headers={"X-Request-Id": UUID})
    assert response.status_code == 404
    assert response.json() == {"detail": "Not found", "request_id": UUID}
    assert response.headers["X-Request-Id"] == UUID


def test_merged_app_returns_the_request_id_on_errors():
    """Both handlers of the merged app, including the one for unhandled exceptions,
    which runs in ServerErrorMiddleware, outside RequestIdMiddleware."""
    from transcribe_api.api.app import create_app

    app = create_app()

    @app.get("/__test_boom")
    def _boom():
        message = "boom"
        raise ValueError(message)

    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/__test_boom", headers={"X-Request-Id": UUID})
    assert response.status_code == 500
    assert response.headers["X-Request-Id"] == UUID
    assert response.json() == {"detail": "Internal Server Error", "request_id": UUID}

    response = client.get("/api/v1/jobs/not-a-uuid", headers={"X-Request-Id": UUID})
    assert response.status_code in (401, 403, 404, 422)
    assert response.headers["X-Request-Id"] == UUID
