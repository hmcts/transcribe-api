"""Preview-only auth bypass (DIAAT-399), under the BenchNotes token (DIAAT-373).

PR previews run the web app with sign-in off, so no Entra token is sent. With
AUTH_PREVIEW_USER_ENABLED, a sign-in (``POST /api/auth/session``) with no token is a fixed
preview user, but only when ENVIRONMENT=preview; anywhere else the process refuses to
start. The preview user is given every role (``sessions.ensure_preview_assignments``) and
then needs a BenchNotes token like anyone else: every other route still refuses a request
with none.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.unit import auth_fixtures as af
from tests.unit.auth_fixtures import CLIENT, TENANT, StubJwks
from tests.unit.test_token_auth import _token
from transcribe_api.domain.auth import token_auth
from transcribe_api.domain.auth.token_auth import get_entra_identity, get_token_auth_user
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
    get_jwt_service().jwks_client = StubJwks()

    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user=Depends(get_entra_identity)):
        return user.model_dump()

    @app.get("/other-route")
    async def other_route(principal=Depends(get_token_auth_user)):
        return {"sub": principal.sub}

    yield TestClient(app)

    get_auth_settings.cache_clear()
    get_jwt_service.cache_clear()


def test_no_token_resolves_to_the_preview_user_at_sign_in(client):
    from hmcts_azure_auth.roles import get_valid_roles

    r = client.get("/whoami")
    assert r.status_code == 200
    assert r.json() == {
        "user_id": token_auth.PREVIEW_USER_ID,
        "name": token_auth.PREVIEW_USER_NAME,
        "email": token_auth.PREVIEW_USER_EMAIL,
        "roles": list(get_valid_roles().values()),
    }


def test_every_other_route_still_needs_a_benchnotes_token_in_preview(client):
    assert client.get("/other-route").status_code == 401


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


# The preview user signs in and is given every role ----------------------------------------


@pytest.fixture
def session_client(jurisdiction_db, monkeypatch):
    from transcribe_api.api import routes_session
    from transcribe_api.api.routes_session import router as session_router

    af.stub_entra(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "preview")
    monkeypatch.setenv("AUTH_PREVIEW_USER_ENABLED", "true")
    monkeypatch.setattr(routes_session, "get_engine", lambda: jurisdiction_db)
    app = FastAPI()
    app.include_router(session_router, prefix="/api")
    yield TestClient(app, raise_server_exceptions=False)
    af.unstub_entra()


@pytest.mark.real_jurisdictions
def test_the_preview_user_signs_in_with_no_token_and_holds_every_role(session_client, jurisdiction_db):
    from tests.unit.jurisdiction_fixtures import add_jurisdiction

    add_jurisdiction(jurisdiction_db, "IAC")
    add_jurisdiction(jurisdiction_db, "ET")

    response = session_client.post("/api/auth/session")

    assert response.status_code == 200, response.text
    me = response.json()["me"]
    assert me["user"]["email"] == token_auth.PREVIEW_USER_EMAIL
    # Several roles were given, so they choose, as anyone with several does.
    assert me["status"] == "choose"
    roles = {(a["jurisdiction"] and a["jurisdiction"]["code"], a["role"]) for a in me["assignments"]}
    assert (None, "JurisdictionSuperAdministrator") in roles
    for code in ("IAC", "ET"):
        assert {(code, "Judge"), (code, "LegalTextManager"), (code, "JurisdictionAdministrator")} <= roles
    # Every role the app has, so the previews' areas are all reachable: and the system one.
    token = response.json()["token"]
    pick = session_client.put(
        "/api/me/context", json={"jurisdiction": "IAC", "role": "Judge"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert pick.status_code == 200
    assert "dictation.use" in pick.json()["me"]["permissions"]
    assert "feature_flags.manage" not in pick.json()["me"]["permissions"]  # a Judge's, and no more
    assert (None, "SystemAdministrator") in roles  # the SystemAdministrator role too, as a context of its own
    system = session_client.put(
        "/api/me/context",
        json={"jurisdiction": None, "role": "SystemAdministrator"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert "feature_flags.manage" in system.json()["me"]["permissions"]

    # Signing in again changes nothing and duplicates nothing.
    again = session_client.post("/api/auth/session")
    assert len(again.json()["me"]["assignments"]) == len(me["assignments"])


@pytest.mark.real_jurisdictions
@pytest.mark.parametrize("environment", ["aat", "prod", "demo", "test"])
def test_outside_preview_a_sign_in_with_no_token_is_401_and_gives_nobody_a_role(
    session_client, jurisdiction_db, monkeypatch, environment
):
    from sqlmodel import Session, select

    from transcribe_api.domain.models_dictation import RoleAssignment

    monkeypatch.setenv("ENVIRONMENT", environment)
    assert session_client.post("/api/auth/session").status_code == 401
    with Session(jurisdiction_db) as session:
        assert session.exec(select(RoleAssignment)).all() == []


@pytest.mark.real_jurisdictions
def test_a_real_sign_in_in_a_preview_is_not_given_every_role(session_client, jurisdiction_db):
    """Only the no-token sign-in is the preview user: a token holder is who the token says."""
    from tests.unit.jurisdiction_fixtures import add_jurisdiction

    add_jurisdiction(jurisdiction_db, "IAC")
    response = session_client.post("/api/auth/session", headers={"Authorization": f"Bearer {_token()}"})
    assert response.status_code == 200
    assert response.json()["me"]["status"] == "no_access"
