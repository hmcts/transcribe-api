"""Token-only authentication (domain/auth/token_auth.py).

Two kinds of token, one for each side of the sign-in (DIAAT-373): the Entra ID token, accepted
only by ``get_entra_identity`` (the sign-in), and the BenchNotes access token, accepted only by
``get_token_auth_user`` (everything else). The Entra tests use real RS256 tokens signed with a
throwaway key and stub only the JWKS lookup, so the library's own verifier (audience, issuer,
expiry, signature) is what is under test.
"""

from __future__ import annotations

import base64
import json
import time
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from tests.unit import auth_fixtures as af
from tests.unit.auth_fixtures import CLIENT, ISSUER, OTHER_KEY, TENANT  # noqa: F401
from transcribe_api.domain.auth import app_token
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.token_auth import get_entra_identity, get_token_auth_user


def _token(key=af.ENTRA_KEY, **overrides) -> str:
    """An Entra ID token for a judge, with the claims the library reads."""
    claims = {"oid": "aaaaaaaa-0000-0000-0000-000000000001", "roles": ["Judge"]}
    return af.entra_token(key=key, **(claims | overrides))


def _principal(email: str, oid: str) -> str:
    """An Easy Auth style X-Ms-Client-Principal header, as an attacker would forge it."""
    body = {
        "auth_typ": "aad",
        "claims": [
            {"typ": "preferred_username", "val": email},
            {"typ": "http://schemas.microsoft.com/identity/claims/objectidentifier", "val": oid},
            {"typ": "name", "val": "Someone Else"},
        ],
    }
    return base64.b64encode(json.dumps(body).encode()).decode()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    af.stub_entra(monkeypatch)

    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user=Depends(get_entra_identity)):
        return user.model_dump()

    @app.get("/principal")
    async def principal(user=Depends(get_token_auth_user)):
        return {
            "sub": str(user.user_id),
            "sysadmin": user.sysadmin,
            "assignments": [a.to_claim() for a in user.assignments],
            "ctx": user.ctx.to_claim() if user.ctx else None,
        }

    yield TestClient(app)

    af.unstub_entra()


def test_valid_token_authenticates_with_identity_from_claims(client):
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200
    assert r.json() == {
        "user_id": "aaaaaaaa-0000-0000-0000-000000000001",
        "name": "Judge Real",
        "email": "judge.real@justice.gov.uk",
        "roles": ["Judge"],
    }


def test_no_token_is_401(client):
    r = client.get("/whoami")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_forged_easy_auth_header_alone_is_401(client):
    """The whole point: on AKS nothing strips this header, so it must be worthless."""
    r = client.get("/whoami", headers={"X-Ms-Client-Principal": _principal("victim@justice.gov.uk", "x")})
    assert r.status_code == 401


def test_email_comes_from_the_token_not_a_forged_header(client):
    """A caller with a valid token cannot claim someone else's email via the header."""
    r = client.get(
        "/whoami",
        headers={
            "Authorization": f"Bearer {_token()}",
            "X-Ms-Client-Principal": _principal("victim@justice.gov.uk", "aaaaaaaa-0000-0000-0000-000000000001"),
        },
    )
    assert r.status_code == 200
    assert r.json()["email"] == "judge.real@justice.gov.uk"


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"aud": "some-other-app"}, id="wrong-audience"),
        pytest.param({"iss": "https://login.microsoftonline.com/other-tenant/v2.0"}, id="wrong-issuer"),
        pytest.param({"exp": int(time.time()) - 60}, id="expired"),
    ],
)
def test_invalid_claims_are_401(client, bad):
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(**bad)}"})
    assert r.status_code == 401


def test_token_signed_by_another_key_is_401(client):
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(key=OTHER_KEY)}"})
    assert r.status_code == 401


def test_token_without_oid_is_401(client):
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(oid='')}"})
    assert r.status_code == 401


def test_unverified_token_never_authenticates_even_when_verification_is_off(client, monkeypatch):
    """The library returns None when verification is disabled; we must not treat that as success."""
    from hmcts_azure_auth.jwt import get_jwt_service
    from hmcts_azure_auth.models import get_auth_settings

    monkeypatch.setenv("JWT_ENABLE_VERIFICATION", "false")
    get_auth_settings.cache_clear()
    get_jwt_service.cache_clear()
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 401


def test_non_strict_mode_does_not_let_a_bad_token_through(client, monkeypatch):
    from hmcts_azure_auth.jwt import get_jwt_service
    from hmcts_azure_auth.models import get_auth_settings

    monkeypatch.setenv("JWT_VERIFICATION_STRICT", "false")
    get_auth_settings.cache_clear()
    get_jwt_service.cache_clear()
    get_jwt_service().jwks_client = af.StubJwks()
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(aud='some-other-app')}"})
    assert r.status_code == 401


def test_local_mode_has_no_mock_identity_any_more(client, monkeypatch):
    """DIAAT-373: local development signs in as a persona (POST /api/auth/session), through the same
    mint as everywhere else, not as an identity with every role."""
    monkeypatch.setenv("ENVIRONMENT", "local")
    assert client.get("/whoami").status_code == 401
    assert client.get("/principal").status_code == 401


APP_ROLES = "Judge,LegalTextManager,SystemAdministrator"


def test_access_roles_set_and_none_held_is_403(client, monkeypatch):
    """With a dedicated registration, a caller must hold one of the app's roles."""
    monkeypatch.setenv("AUTH_ACCESS_ROLES", APP_ROLES)
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(roles=[])}"})
    assert r.status_code == 403


def test_access_roles_set_and_one_held_authenticates(client, monkeypatch):
    monkeypatch.setenv("AUTH_ACCESS_ROLES", APP_ROLES)
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(roles=['LegalTextManager'])}"})
    assert r.status_code == 200


def test_access_roles_ignores_roles_outside_the_list(client, monkeypatch):
    monkeypatch.setenv("AUTH_ACCESS_ROLES", "Judge, LegalTextManager")
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(roles=['Contributor', 'SomethingElse'])}"})
    assert r.status_code == 403


def test_blank_access_roles_are_not_checked(client, monkeypatch):
    monkeypatch.setenv("AUTH_ACCESS_ROLES", " , ")
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(roles=[])}"})
    assert r.status_code == 200


def test_the_access_roles_gate_is_the_sign_ins_not_the_app_tokens(client, monkeypatch):
    """The Entra gate (AUTH_ACCESS_ROLES) guards the sign-in. A BenchNotes token carries no Entra roles."""
    monkeypatch.setenv("AUTH_ACCESS_ROLES", APP_ROLES)
    token, _ = app_token.mint(uuid4(), sysadmin=False, assignments=[], ctx=None)
    assert client.get("/principal", headers={"Authorization": f"Bearer {token}"}).status_code == 200


# --- the BenchNotes access token ----------------------------------------------------------

JUDGE_ET = Context("ET", "Judge")


def test_a_benchnotes_token_authenticates_with_what_it_says(client):
    user_id = uuid4()
    token, _ = app_token.mint(
        user_id, sysadmin=True, assignments=[JUDGE_ET, Context(None, "JurisdictionSuperAdministrator")], ctx=JUDGE_ET
    )
    response = client.get("/principal", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {
        "sub": str(user_id),
        "sysadmin": True,
        "assignments": [
            {"jurisdiction": "ET", "role": "Judge"},
            {"jurisdiction": None, "role": "JurisdictionSuperAdministrator"},
        ],
        "ctx": {"jurisdiction": "ET", "role": "Judge"},
    }


def test_an_entra_token_is_not_a_benchnotes_token(client):
    response = client.get("/principal", headers={"Authorization": f"Bearer {_token()}"})
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"


def test_a_benchnotes_token_is_not_an_entra_token(client):
    token, _ = app_token.mint(uuid4(), sysadmin=False, assignments=[], ctx=None)
    assert client.get("/whoami", headers={"Authorization": f"Bearer {token}"}).status_code == 401


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer ", "Basic abc", "bearer lower-case-scheme"],
)
def test_a_missing_or_malformed_authorization_header_is_401(client, header):
    headers = {} if header is None else {"Authorization": header}
    assert client.get("/principal", headers=headers).status_code == 401


def test_an_expired_or_forged_benchnotes_token_is_401(client):
    expired, _ = app_token.mint(uuid4(), sysadmin=False, assignments=[], ctx=None, now=time.time() - 1000)
    forged = af.forged_app_token({"sysadmin": True}, key="another-key-another-key-another-key-0000")
    wrong_audience = af.forged_app_token({"aud": CLIENT})
    for token in (expired, forged, wrong_audience):
        assert client.get("/principal", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_a_benchnotes_token_naming_system_administrator_as_a_context_is_401(client):
    token = af.forged_app_token({"ctx": {"jurisdiction": None, "role": "SystemAdministrator"}})
    assert client.get("/principal", headers={"Authorization": f"Bearer {token}"}).status_code == 401
