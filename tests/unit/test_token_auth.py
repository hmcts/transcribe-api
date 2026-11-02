"""Token-only authentication (domain/auth/token_auth.py).

These use real RS256 tokens signed with a throwaway key, and stub only the JWKS
lookup, so the library's own verifier (audience, issuer, expiry, signature) is
what is under test.
"""

from __future__ import annotations

import base64
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from transcribe_api.domain.auth import token_auth
from transcribe_api.domain.auth.token_auth import get_token_auth_user

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _StubJwks:
    """Stands in for PyJWKClient: always returns our test public key."""

    def get_signing_key_from_jwt(self, _token):
        class _Key:
            key = _KEY.public_key()

        return _Key()


def _token(key=_KEY, **overrides) -> str:
    now = int(time.time())
    claims = {
        "aud": CLIENT,
        "iss": ISSUER,
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "oid": "aaaaaaaa-0000-0000-0000-000000000001",
        "preferred_username": "judge.real@justice.gov.uk",
        "name": "Judge Real",
        "roles": ["Judge"],
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256")


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
    from hmcts_azure_auth.jwt import get_jwt_service
    from hmcts_azure_auth.models import get_auth_settings

    monkeypatch.setenv("ENVIRONMENT", "test")
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
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(key=_OTHER_KEY)}"})
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
    get_jwt_service().jwks_client = _StubJwks()
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(aud='some-other-app')}"})
    assert r.status_code == 401


def test_local_development_uses_the_mock_identity(client, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "local")
    r = client.get("/whoami")
    assert r.status_code == 200
    assert r.json()["user_id"] == token_auth.LOCAL_DEV_USER_ID


APP_ROLES = "Judge,LegalTextManager,SystemAdministrator,Normal"


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
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(roles=['Normal', 'SomethingElse'])}"})
    assert r.status_code == 403


def test_blank_access_roles_are_not_checked(client, monkeypatch):
    monkeypatch.setenv("AUTH_ACCESS_ROLES", " , ")
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(roles=[])}"})
    assert r.status_code == 200


def test_user_without_an_access_role_is_never_resolved(client, monkeypatch):
    """No application user row is created for someone refused at the door."""
    monkeypatch.setenv("AUTH_ACCESS_ROLES", APP_ROLES)
    resolved: list[str] = []

    def resolver(azure_user_id: str, email: str, roles: list[str]):
        resolved.append(azure_user_id)
        return {"id": azure_user_id}

    token_user = token_auth.build_token_user_dep(resolver)

    @client.app.get("/me")
    async def me(user=Depends(token_user)):
        return user

    r = client.get("/me", headers={"Authorization": f"Bearer {_token(roles=[])}"})
    assert r.status_code == 403
    assert resolved == []
