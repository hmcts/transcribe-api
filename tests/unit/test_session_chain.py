"""A context re-mint never extends what the last Entra verification vouched for (review finding S2).

``PUT /api/me/context`` and ``DELETE /api/me/jurisdictions/{code}`` mint a token from the presenting
BenchNotes token, not from Entra, and copy its ``sysadmin`` claim. Without a bound a holder could
call either every few minutes and keep ``sysadmin`` for ever after the Entra role was removed. The
token now carries ``sess_exp`` (sign-in time + one lifetime), set only at ``POST /api/auth/session``,
and a re-minted token never outlives it.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest

from tests.unit import auth_fixtures as af
from tests.unit.admin_fixtures import ADMIN, JUDGE, make_world
from transcribe_api.domain.auth import app_token

pytestmark = pytest.mark.real_jurisdictions

TTL = 300


@pytest.fixture
def world(jurisdiction_db, monkeypatch):
    yield make_world(jurisdiction_db, monkeypatch)
    af.unstub_entra()


@pytest.fixture
def clock(monkeypatch):
    """The token minter's clock, which can be set in the past (a token minted in the future would be
    refused by the verifier's own clock). ``clock.at(-200)``: 200 seconds before now."""
    state = {"now": None}
    monkeypatch.setattr(app_token, "time", SimpleNamespace(time=lambda: state["now"] if state["now"] else time.time()))

    class Clock:
        def at(self, offset: float) -> None:
            state["now"] = time.time() + offset

        def real(self) -> None:
            state["now"] = None

    return Clock()


def claims(token: str) -> dict:
    return jwt.decode(token, options={"verify_signature": False})


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_a_sign_in_starts_a_session_that_ends_with_the_token(world):
    boss = world.person("Boss", ("IAC", ADMIN), sysadmin=True)
    token = boss.sign_in(("IAC", ADMIN)).json()["token"]

    body = claims(token)
    assert body["sess_exp"] == body["exp"] == body["iat"] + TTL


def test_re_minting_the_context_never_extends_the_session(world, clock):
    boss = world.person("Boss", ("IAC", ADMIN), ("ET", JUDGE), sysadmin=True)
    clock.at(-250)
    first = boss.sign_in(("IAC", ADMIN)).json()["token"]
    sess_exp = claims(first)["sess_exp"]

    token = first
    for offset in (-200, -120, -60, -10):
        clock.at(offset)
        response = world.client.put(
            "/api/me/context", headers=bearer(token), json={"jurisdiction": "ET", "role": JUDGE}
        )
        assert response.status_code == 200, response.text
        token = response.json()["token"]
        minted = claims(token)
        # Carried over, and the new token ends there, however long ago it was minted: not a full lifetime on.
        assert minted["sess_exp"] == sess_exp
        assert minted["exp"] == min(minted["iat"] + TTL, sess_exp) == sess_exp
        assert response.json()["expires_at"].startswith(time.strftime("%Y-%m-%d", time.gmtime(sess_exp)))
        assert minted["sysadmin"] is True  # still carried: until the session ends


def test_leaving_a_jurisdiction_never_extends_the_session_either(world, clock):
    boss = world.person("Boss", ("IAC", ADMIN), ("ET", JUDGE), sysadmin=True)
    clock.at(-250)
    first = boss.sign_in(("IAC", ADMIN)).json()["token"]
    clock.at(-100)

    response = world.client.delete("/api/me/jurisdictions/ET", headers=bearer(first))

    assert response.status_code == 200, response.text
    assert claims(response.json()["token"])["exp"] == claims(first)["exp"]


def test_when_the_chain_has_run_out_the_token_is_401_and_a_sign_in_starts_afresh(world, clock):
    boss = world.person("Boss", ("IAC", ADMIN), ("ET", JUDGE), sysadmin=True)
    # Signed in 4 min 55 s ago: five seconds of the session left.
    clock.at(-295)
    first = boss.sign_in(("IAC", ADMIN)).json()["token"]
    clock.real()
    in_a_moment = world.client.put("/api/me/context", headers=bearer(first), json={"jurisdiction": "ET", "role": JUDGE})
    assert in_a_moment.status_code == 200
    reminted = in_a_moment.json()["token"]
    assert claims(reminted)["exp"] == claims(first)["exp"]  # five seconds, not five minutes

    # After that moment neither token opens a no-context route: the web re-mints from Entra (401).
    expired = af.forged_app_token(
        {"sub": claims(first)["sub"], "iat": claims(first)["iat"], "exp": int(time.time()) - 1}, key=af.APP_KEY
    )
    assert world.client.put("/api/me/context", headers=bearer(expired), json={"jurisdiction": "ET", "role": JUDGE}).status_code == 401
    assert world.client.delete("/api/me/jurisdictions/ET", headers=bearer(expired)).status_code == 401
    assert world.client.get("/api/me/permissions", headers=bearer(expired)).status_code == 401

    # Signing in again (Entra) is a new session.
    fresh = claims(boss.sign_in(("IAC", ADMIN)).json()["token"])
    assert fresh["sess_exp"] == fresh["exp"] > claims(first)["exp"]


def test_the_sysadmin_claim_ends_with_the_session_even_if_the_user_keeps_calling(world, clock):
    boss = world.person("Boss", ("IAC", ADMIN), sysadmin=True)
    clock.at(-299)
    token = boss.sign_in(("IAC", ADMIN)).json()["token"]
    clock.real()
    for _ in range(3):  # "every few minutes": here, back to back
        response = world.client.put("/api/me/context", headers=bearer(token), json={"jurisdiction": "IAC", "role": ADMIN})
        assert response.status_code == 200
        token = response.json()["token"]
    assert claims(token)["exp"] <= int(time.time()) + 2
    # (The one-second-lifetime token is about to be refused; one that has run out is, whatever it claims.)
    time.sleep(2.2)
    assert world.client.get("/api/admin/feature-flags", headers=bearer(token)).status_code == 401


def test_the_session_claim_is_an_integer_and_a_token_without_it_is_its_own_session():
    token, _ = app_token.mint("11111111-1111-1111-1111-111111111111", sysadmin=False, assignments=[], ctx=None)
    assert app_token.verify(token).session_expires_at == app_token.verify(token).expires_at
    old = af.forged_app_token()  # a token minted before the claim existed
    principal = app_token.verify(old)
    assert principal.session_expires_at == principal.expires_at
    with pytest.raises(app_token.AppTokenError):
        app_token.verify(af.forged_app_token({"sess_exp": "tomorrow"}))
    with pytest.raises(app_token.AppTokenError):
        app_token.verify(af.forged_app_token({"sess_exp": True}))


def test_a_token_without_exp_is_refused_whatever_else_it_says(world):
    """(D3) ``exp`` is required by the decoder itself, not only by the claims read after it."""
    from uuid import uuid4

    forged = af.forged_app_token({"sub": str(uuid4())}, drop=("exp",))
    with pytest.raises(app_token.AppTokenError):
        app_token.verify(forged)
    assert world.client.get("/api/me/permissions", headers=bearer(forged)).status_code == 401
