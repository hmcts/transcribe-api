"""Fixtures shared by the dictation route tests.

Defined here rather than imported from another test module: pytest finds conftest
fixtures by name, while an imported fixture looks unused to linters and their
autofixes, which then remove the import. A module that defines its own fixture of the
same name keeps it.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app():
    import transcribe_api.api.routes_dictation

    test_app = FastAPI()
    test_app.include_router(transcribe_api.api.routes_dictation.router, prefix="/api")
    test_app.include_router(transcribe_api.api.routes_dictation.telemetry_router, prefix="/api")
    return test_app


@pytest.fixture
def client(app):
    """Server exceptions are returned as 500s, not raised: tests assert on them."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def as_user(app):
    """Inject a User into get_current_user for the duration of a test."""
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user

    yield lambda user: app.dependency_overrides.__setitem__(get_current_user, lambda: user)
    app.dependency_overrides.pop(get_current_user, None)
