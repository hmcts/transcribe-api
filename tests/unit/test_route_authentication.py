"""Every route authenticates with the BenchNotes token (DIAAT-373, DIAAT-386).

Structural, over the routes of the real application (as api/app.py mounts them): once the Entra
role gate (AUTH_ACCESS_ROLES) is emptied, so that a tenant member with no role can sign in, nothing but
what is named here may be reachable without a BenchNotes token, and nothing that authenticates
a user may be reached without a context unless it is named here too. A new route that forgets
authentication, or that skips the context check, fails this test rather than going live open.
Who may call each route is the next layer: tests/unit/test_permission_matrix.py (DIAAT-376).
"""

from __future__ import annotations

import pytest

from tests.unit.test_permission_matrix import Route, routes_of
from transcribe_api.domain.auth import dependencies_dictation, dependencies_recording
from transcribe_api.domain.auth.token_auth import get_token_auth_user

# No BenchNotes token: health, version, the sign-in itself (which authenticates by the Entra token
# or, locally, a persona), and local audio, which Azure Speech fetches without our token (a signed,
# expiring URL: tests/unit/recording/unit/api/test_routes.py::TestLocalAudio; 404 unless
# AUDIO_STORAGE_BACKEND=local). The OpenAPI document is not an API route.
OPEN = {
    ("GET", "/health"),
    ("GET", "/api/health"),
    ("GET", "/api/healthcheck"),
    ("GET", "/api/health/dependencies"),
    ("POST", "/api/auth/session"),
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/version"),
    ("GET", "/api/v1/local-audio/{blob_name}"),
}
# Authenticated with a user's BenchNotes token but needing no context (any signed-in user).
NO_CONTEXT = {
    ("GET", "/api/me/permissions"),
    ("PUT", "/api/me/context"),
    ("DELETE", "/api/me/jurisdictions/{code}"),
    ("GET", "/api/jurisdictions/public"),
    ("GET", "/api/flags"),
}
# These routes used to be open and are not any more (DIAAT-376): the storage probe needs system.settings.
FORMERLY_OPEN = {("GET", "/api/healthcheck/azure-storage")}


@pytest.fixture(scope="module")
def routes() -> list[Route]:
    from transcribe_api.api.app import create_app

    found, _plain = routes_of(create_app())
    assert len(found) > 80  # not vacuous
    return found


def test_every_route_authenticates_the_user_with_the_benchnotes_token_or_is_named_here(routes):
    unauthenticated = [
        route.key for route in routes if get_token_auth_user not in route.calls() and route.key not in OPEN
    ]
    assert unauthenticated == [], f"routes with no authentication, not named in OPEN: {unauthenticated}"


def test_every_route_that_authenticates_a_user_needs_a_context_unless_it_is_a_no_context_route(routes):
    contexts_required = {dependencies_dictation.get_current_user, dependencies_recording.get_current_user}
    lenient = {dependencies_dictation.get_signed_in_user}
    for route in routes:
        calls = route.calls()
        if get_token_auth_user not in calls:
            continue
        if route.key in NO_CONTEXT:
            assert any(call in lenient for call in calls), f"{route.key} should be a no-context route"
            continue
        assert any(call in contexts_required for call in calls), (
            f"{route.key} authenticates a user but does not need a context, and is not in NO_CONTEXT"
        )


def test_the_named_routes_exist(routes):
    paths = {route.key for route in routes}
    assert NO_CONTEXT <= paths
    assert OPEN <= paths
    assert FORMERLY_OPEN <= paths


def test_the_routes_that_were_open_before_the_permission_sweep_authenticate_now(routes):
    by_key = {route.key: route for route in routes}
    for key in FORMERLY_OPEN:
        assert get_token_auth_user in by_key[key].calls(), key
