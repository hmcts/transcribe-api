"""Preview-only auth bypass (DIAAT-399).

PR previews run the web app with sign-in off, so the API is called without a
token. AUTH_PREVIEW_USER_ENABLED lets those calls through as a fixed preview
user, but only when ENVIRONMENT=preview; anywhere else the process refuses to
start.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.unit.test_token_auth import CLIENT, TENANT, _StubJwks, _token
from transcribe_api.domain.auth import token_auth
from transcribe_api.domain.auth.token_auth import get_token_auth_user
from transcribe_api.runtime.settings_dictation import get_settings


@pytest.fixture
def client(monkeypatch):
    from hmcts_azure_auth.jwt import get_jwt_service
    from hmcts_azure_auth.models import get_auth_settings

    monkeypatch.setenv("ENVIRONMENT", "preview")
    monkeypatch.setenv("AUTH_PREVIEW_USER_ENABLED", "true")
    monkeypatch.delenv("AUTH_PREVIEW_USER_ROLES", raising=False)
    monkeypatch.delenv("AUTH_ACCESS_ROLES", raising=False)
    monkeypatch.setenv("AZURE_AD_TENANT_ID", TENANT)
    monkeypatch.setenv("AZURE_AD_CLIENT_ID", CLIENT)
    monkeypatch.setenv("JWT_ENABLE_VERIFICATION", "true")
    monkeypatch.setenv("JWT_VERIFICATION_STRICT", "true")
    get_auth_settings.cache_clear()
    get_jwt_service.cache_clear()
    get_jwt_service().jwks_client = _StubJwks()

    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user=Depends(get_token_auth_user)):
        return user.model_dump()

    yield TestClient(app)

    get_auth_settings.cache_clear()
    get_jwt_service.cache_clear()


def test_no_token_resolves_to_the_preview_user(client):
    from hmcts_azure_auth.roles import get_valid_roles

    r = client.get("/whoami")
    assert r.status_code == 200
    assert r.json() == {
        "user_id": token_auth.PREVIEW_USER_ID,
        "name": token_auth.PREVIEW_USER_NAME,
        "email": token_auth.PREVIEW_USER_EMAIL,
        "roles": list(get_valid_roles().values()),
    }


def test_preview_user_roles_are_configurable(client, monkeypatch):
    monkeypatch.setenv("AUTH_PREVIEW_USER_ROLES", " LegalTextManager , Normal ")
    r = client.get("/whoami")
    assert r.status_code == 200
    assert r.json()["roles"] == ["LegalTextManager", "Normal"]


@pytest.mark.parametrize("flag", ["false", "", "0"])
def test_flag_off_in_preview_still_requires_a_token(client, monkeypatch, flag):
    monkeypatch.setenv("AUTH_PREVIEW_USER_ENABLED", flag)
    assert client.get("/whoami").status_code == 401


@pytest.mark.parametrize("environment", ["aat", "prod", "demo", "test", "local-ish"])
def test_flag_outside_preview_never_bypasses(client, monkeypatch, environment):
    monkeypatch.setenv("ENVIRONMENT", environment)
    assert client.get("/whoami").status_code == 401


def test_preview_user_without_an_access_role_is_403(client, monkeypatch):
    monkeypatch.setenv("AUTH_ACCESS_ROLES", "Judge,LegalTextManager")
    monkeypatch.setenv("AUTH_PREVIEW_USER_ROLES", "Normal")
    assert client.get("/whoami").status_code == 403


def test_preview_user_with_an_access_role_is_allowed(client, monkeypatch):
    monkeypatch.setenv("AUTH_ACCESS_ROLES", "Judge,LegalTextManager")
    monkeypatch.setenv("AUTH_PREVIEW_USER_ROLES", "Normal,Judge")
    assert client.get("/whoami").status_code == 200


def test_a_supplied_token_is_still_verified(client):
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200
    assert r.json()["user_id"] == "aaaaaaaa-0000-0000-0000-000000000001"


@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param(f"Bearer {_token(aud='some-other-app')}", id="invalid-token"),
        pytest.param("Bearer ", id="empty-bearer"),
        pytest.param("Basic dXNlcjpwYXNz", id="not-bearer"),
    ],
)
def test_a_bad_authorization_header_is_401_not_the_preview_user(client, authorization):
    assert client.get("/whoami", headers={"Authorization": authorization}).status_code == 401


def test_settings_accept_the_flag_in_preview(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "preview")
    monkeypatch.setenv("AUTH_PREVIEW_USER_ENABLED", "true")
    assert get_settings().AUTH_PREVIEW_USER_ENABLED is True


@pytest.mark.parametrize("environment", ["aat", "prod", "demo", "local", "test", "dev"])
def test_settings_refuse_the_flag_outside_preview(monkeypatch, environment):
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("AUTH_PREVIEW_USER_ENABLED", "true")
    with pytest.raises(ValidationError, match="AUTH_PREVIEW_USER_ENABLED"):
        get_settings()


@pytest.mark.parametrize("environment", ["aat", "prod"])
def test_app_refuses_to_start_with_the_flag_outside_preview(monkeypatch, environment):
    from transcribe_api.api.app import create_app

    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("AUTH_PREVIEW_USER_ENABLED", "true")
    with pytest.raises(ValidationError):
        create_app()


def test_app_warns_at_startup_when_the_preview_user_is_on(monkeypatch, caplog):
    from transcribe_api.api.app import create_app

    monkeypatch.setenv("ENVIRONMENT", "preview")
    monkeypatch.setenv("AUTH_PREVIEW_USER_ENABLED", "true")
    with caplog.at_level(logging.WARNING):
        create_app()
    assert any("AUTH_PREVIEW_USER_ENABLED" in r.getMessage() for r in caplog.records)


def test_preview_settings_behave_like_a_deployed_environment(monkeypatch):
    from transcribe_api.api.app import LOCAL_DEV_ORIGINS, build_cors_origins
    from transcribe_api.runtime.settings_dictation import ProductionSettings

    monkeypatch.setenv("ENVIRONMENT", "preview")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://transcribe-web-pr-1.preview.platform.hmcts.net")
    settings = get_settings()
    assert isinstance(settings, ProductionSettings)
    assert build_cors_origins(settings) != LOCAL_DEV_ORIGINS


def test_only_the_preview_values_turn_the_preview_user_on():
    from pathlib import Path

    chart = Path(__file__).resolve().parents[2] / "charts" / "transcribe-api"
    values_files = sorted(chart.glob("values*.yaml"))
    assert chart / "values.preview.template.yaml" in values_files
    for values_file in values_files:
        if values_file.name != "values.preview.template.yaml":
            assert "AUTH_PREVIEW_USER_ENABLED" not in values_file.read_text(), values_file.name
