"""Authentication for tests (DIAAT-373): Entra ID tokens the real verifier accepts, and the
BenchNotes tokens and sign-ins the API issues.

``stub_entra`` points the shared auth library at a throwaway RSA key (only the JWKS lookup is
stubbed: signature, audience, issuer and expiry are really checked). ``entra_token`` signs a
token with it; ``forged_app_token`` signs a BenchNotes token with any claims, for the tokens
that must be refused.
"""

from __future__ import annotations

import time
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"

ENTRA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

APP_KEY = "test-benchnotes-signing-key-0123456789abcdef"  # pragma: allowlist secret


class StubJwks:
    """Stands in for PyJWKClient: always returns our test public key."""

    def get_signing_key_from_jwt(self, _token):
        class _Key:
            key = ENTRA_KEY.public_key()

        return _Key()


def entra_token(key=ENTRA_KEY, **overrides) -> str:
    now = int(time.time())
    claims = {
        "aud": CLIENT,
        "iss": ISSUER,
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "oid": str(uuid4()),
        "preferred_username": "judge.real@justice.gov.uk",
        "name": "Judge Real",
        "roles": [],
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256")


def stub_entra(monkeypatch) -> None:
    """Make the shared library verify Entra tokens made by ``entra_token``. Call before the first request."""
    from hmcts_azure_auth.jwt import get_jwt_service
    from hmcts_azure_auth.models import get_auth_settings

    monkeypatch.setenv("AZURE_AD_TENANT_ID", TENANT)
    monkeypatch.setenv("AZURE_AD_CLIENT_ID", CLIENT)
    monkeypatch.setenv("JWT_ENABLE_VERIFICATION", "true")
    monkeypatch.setenv("JWT_VERIFICATION_STRICT", "true")
    monkeypatch.delenv("AUTH_ACCESS_ROLES", raising=False)
    get_auth_settings.cache_clear()
    get_jwt_service.cache_clear()
    get_jwt_service().jwks_client = StubJwks()


def unstub_entra() -> None:
    from hmcts_azure_auth.jwt import get_jwt_service
    from hmcts_azure_auth.models import get_auth_settings

    get_auth_settings.cache_clear()
    get_jwt_service.cache_clear()


def forged_app_token(claims_overrides=None, key=APP_KEY, algorithm="HS256", drop=()) -> str:
    """A BenchNotes-shaped token with the claims you give, signed with ``key``."""
    now = int(time.time())
    claims = {
        "iss": "benchnotes-api",
        "aud": "benchnotes",
        "sub": str(uuid4()),
        "iat": now,
        "exp": now + 300,
        "sysadmin": False,
        "assignments": [],
        "ctx": None,
    }
    claims.update(claims_overrides or {})
    for name in drop:
        claims.pop(name)
    return jwt.encode(claims, key, algorithm=algorithm)
