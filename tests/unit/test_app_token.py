"""The BenchNotes access token (domain/auth/app_token.py)."""

from __future__ import annotations

import time
from uuid import uuid4

import jwt
import pytest

from transcribe_api.domain.auth import app_token
from transcribe_api.domain.auth.app_token import AppTokenError, Context
from transcribe_api.runtime.settings_dictation import get_settings

KEY = "test-benchnotes-signing-key-0123456789abcdef"  # pragma: allowlist secret
JUDGE_ET = Context("ET", "Judge")
ADMIN_IAC = Context("IAC", "JurisdictionAdministrator")
SUPER = Context(None, "JurisdictionSuperAdministrator")


def _mint(**overrides):
    args = {"user_id": uuid4(), "sysadmin": False, "assignments": [JUDGE_ET], "ctx": JUDGE_ET} | overrides
    return app_token.mint(args.pop("user_id"), **args)


def _forge(claims_overrides=None, key=KEY, algorithm="HS256", drop=()):
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


def test_a_minted_token_round_trips_its_claims():
    user_id = uuid4()
    token, expires_at = app_token.mint(
        user_id, sysadmin=True, assignments=[JUDGE_ET, ADMIN_IAC, SUPER], ctx=ADMIN_IAC, name="Judge Real"
    )
    principal = app_token.verify(token)
    assert principal.user_id == user_id
    assert principal.sysadmin is True
    assert principal.assignments == (JUDGE_ET, ADMIN_IAC, SUPER)
    assert principal.ctx == ADMIN_IAC
    assert principal.name == "Judge Real"
    assert principal.expires_at == expires_at
    assert (principal.expires_at - principal.issued_at).total_seconds() == 300


def test_the_claims_are_the_contracts():
    token, _ = _mint(assignments=[JUDGE_ET, SUPER], ctx=None, sysadmin=True)
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(token, KEY, algorithms=["HS256"], audience="benchnotes")
    assert header["alg"] == "HS256"
    assert claims["iss"] == "benchnotes-api"
    assert claims["aud"] == "benchnotes"
    assert claims["sysadmin"] is True
    assert claims["assignments"] == [
        {"jurisdiction": "ET", "role": "Judge"},
        {"jurisdiction": None, "role": "JurisdictionSuperAdministrator"},
    ]
    assert claims["ctx"] is None
    # Permissions are not in the token: they are derived from the role.
    assert "permissions" not in claims
    assert set(claims) == {"iss", "aud", "sub", "iat", "exp", "sysadmin", "assignments", "ctx", "name"}


def test_the_lifetime_follows_the_setting(monkeypatch):
    monkeypatch.setenv("APP_JWT_TTL_SECONDS", "60")
    get_settings.cache_clear()
    token, expires_at = _mint()
    principal = app_token.verify(token)
    assert (principal.expires_at - principal.issued_at).total_seconds() == 60
    assert principal.expires_at == expires_at


def test_the_default_lifetime_is_five_minutes(monkeypatch):
    monkeypatch.delenv("APP_JWT_TTL_SECONDS")
    get_settings.cache_clear()
    assert app_token.ttl_seconds() == 300


def test_an_expired_token_is_rejected():
    token, _ = app_token.mint(uuid4(), sysadmin=False, assignments=[], ctx=None, now=time.time() - 301)
    with pytest.raises(AppTokenError):
        app_token.verify(token)


def test_a_token_at_the_edge_of_its_life_is_still_good():
    token, _ = app_token.mint(uuid4(), sysadmin=False, assignments=[], ctx=None, now=time.time() - 290)
    assert app_token.verify(token).ctx is None


def test_a_tampered_token_is_rejected():
    token, _ = _mint(sysadmin=False)
    header, payload, signature = token.split(".")
    forged_payload = _forge({"sysadmin": True, "assignments": [SUPER.to_claim()], "ctx": SUPER.to_claim()}).split(".")[1]
    for tampered in (f"{header}.{forged_payload}.{signature}", f"{header}.{payload}.{signature[:-3]}abc"):
        with pytest.raises(AppTokenError):
            app_token.verify(tampered)


def test_a_token_signed_with_another_key_is_rejected():
    with pytest.raises(AppTokenError):
        app_token.verify(_forge(key="another-key-another-key-another-key-0000"))


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"aud": "some-other-app"}, id="wrong-audience"),
        pytest.param({"iss": "https://login.microsoftonline.com/tenant/v2.0"}, id="wrong-issuer"),
        pytest.param({"exp": int(time.time()) - 5}, id="expired"),
    ],
)
def test_a_token_with_the_wrong_audience_issuer_or_expiry_is_rejected(bad):
    with pytest.raises(AppTokenError):
        app_token.verify(_forge(bad))


@pytest.mark.parametrize("missing", ["aud", "iss", "exp", "iat", "sub"])
def test_a_token_missing_a_required_claim_is_rejected(missing):
    with pytest.raises(AppTokenError):
        app_token.verify(_forge(drop=(missing,)))


def test_an_unsigned_token_and_a_token_in_another_algorithm_are_rejected():
    unsigned = jwt.encode({"sub": str(uuid4()), "aud": "benchnotes", "iss": "benchnotes-api"}, None, algorithm="none")
    with pytest.raises(AppTokenError):
        app_token.verify(unsigned)
    with pytest.raises(AppTokenError):
        app_token.verify(_forge(algorithm="HS512"))


def test_garbage_is_rejected():
    for garbage in ("", "x", "a.b.c", "Bearer abc"):
        with pytest.raises(AppTokenError):
            app_token.verify(garbage)


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"sub": "not-a-uuid"}, id="sub"),
        pytest.param({"sysadmin": "yes"}, id="sysadmin-not-a-bool"),
        pytest.param({"assignments": "ET:Judge"}, id="assignments-not-a-list"),
        pytest.param({"assignments": [{"jurisdiction": "ET", "role": "SystemAdministrator"}]}, id="assignment-sysadmin"),
        pytest.param({"assignments": [{"jurisdiction": "ET", "role": "Normal"}]}, id="assignment-normal"),
        pytest.param({"assignments": ["Judge"]}, id="assignment-not-an-object"),
        pytest.param({"ctx": {"jurisdiction": "ET", "role": "SystemAdministrator"}}, id="ctx-sysadmin"),
        pytest.param({"ctx": {"jurisdiction": 5, "role": "Judge"}}, id="ctx-jurisdiction"),
        pytest.param({"ctx": "Judge"}, id="ctx-not-an-object"),
    ],
)
def test_malformed_claims_are_rejected_even_when_signed(claims):
    with pytest.raises(AppTokenError):
        app_token.verify(_forge(claims))


def test_system_administrator_is_never_a_context_or_an_assignment():
    with pytest.raises(AppTokenError):
        Context.from_claim({"jurisdiction": None, "role": "SystemAdministrator"})


def test_a_token_without_a_context_has_a_null_ctx():
    token, _ = _mint(assignments=[JUDGE_ET, ADMIN_IAC], ctx=None)
    assert app_token.verify(token).ctx is None


class TestTheSigningKey:
    def test_it_is_required_outside_local_and_test(self):
        assert app_token.signing_key_problem("production", None) == "APP_JWT_SIGNING_KEY is not set"
        assert app_token.signing_key_problem("aat", "") == "APP_JWT_SIGNING_KEY is not set"
        assert "at least" in app_token.signing_key_problem("prod", "short")
        assert app_token.signing_key_problem("prod", KEY) is None

    def test_local_and_test_may_go_without(self):
        for environment in ("local", "test"):
            assert app_token.signing_key_problem(environment, None) is None

    def test_a_deployed_process_without_a_key_cannot_sign(self, monkeypatch):
        monkeypatch.delenv("APP_JWT_SIGNING_KEY")
        monkeypatch.setenv("ENVIRONMENT", "production")
        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="APP_JWT_SIGNING_KEY"):
            _mint()

    def test_local_without_a_key_signs_with_one_for_the_process(self, monkeypatch):
        monkeypatch.delenv("APP_JWT_SIGNING_KEY")
        monkeypatch.setenv("ENVIRONMENT", "local")
        monkeypatch.setattr(app_token, "_ephemeral_key", None)
        get_settings.cache_clear()
        token, _ = _mint()
        assert app_token.verify(token).ctx == JUDGE_ET
        # And it is not the test key, so a token signed with that is not accepted.
        with pytest.raises(AppTokenError):
            app_token.verify(_forge())
