"""Every route authenticates with the BenchNotes token (DIAAT-373, DIAAT-386).

Structural, over the routes of the real application: once the Entra role gate
(AUTH_ACCESS_ROLES) is emptied, so that a tenant member with no role can sign in, nothing but
what is named here may be reachable without a BenchNotes token, and nothing that authenticates
a user may be reached without a context unless it is named here too. A new route that forgets
authentication, or that skips the context check, fails this test rather than going live open.
The permission checks that replace the role-name lists (DIAAT-376) are the next layer.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from transcribe_api.domain.auth import dependencies_dictation, dependencies_recording
from transcribe_api.domain.auth.token_auth import get_token_auth_user

# No authentication: health, version, the sign-in itself (which authenticates by the Entra token
# or, locally, a persona), the machine API-key ingress, and the OpenAPI document.
OPEN = {
    ("GET", "/health"),
    ("GET", "/api/health"),
    ("GET", "/api/healthcheck"),
    ("GET", "/api/version"),
    ("GET", "/api/health/dependencies"),
    ("POST", "/api/auth/session"),
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/version"),
    # Open before DIAAT-373 and still: a storage probe for the pipeline's checks, and local audio that
    # Azure Speech fetches without our token (404 unless AUDIO_STORAGE_BACKEND=local). For the
    # permission sweep (DIAAT-376) to decide whether they stay exempt.
    ("GET", "/api/healthcheck/azure-storage"),
    ("GET", "/api/v1/local-audio/{blob_name:path}"),
}
# Authenticated with a user's BenchNotes token but needing no context (any signed-in user).
NO_CONTEXT = {
    ("GET", "/api/me/permissions"),
    ("PUT", "/api/me/context"),
    ("DELETE", "/api/me/jurisdictions/{code}"),
    ("GET", "/api/jurisdictions/public"),
}
# Authenticated by an API key (a machine caller), not a user.
API_KEY_PREFIXES = ("/api/v1/callers", "/api/v1/transcriptions")


def _tree(route: Mounted) -> list:
    calls = []

    def walk(dependant):
        for dependency in dependant.dependencies:
            calls.append(dependency.call)
            walk(dependency)

    walk(route.dependant)
    return calls


class Mounted:
    """A route with the full path the application serves it at."""

    def __init__(self, route: APIRoute, prefix: str) -> None:
        self.route = route
        self.path = prefix + route.path
        self.methods = route.methods
        self.dependant = route.dependant


@pytest.fixture(scope="module")
def routes():
    """Every route of the application's routers, as api/app.py mounts them (its own routes,
    /health, are named in OPEN)."""
    from transcribe_api.api.routes_dictation import router as dictation
    from transcribe_api.api.routes_dictation import telemetry_router
    from transcribe_api.api.routes_dictation_privileged import router as admin
    from transcribe_api.api.routes_jurisdictions import router as jurisdictions
    from transcribe_api.api.routes_recording import router as recording
    from transcribe_api.api.routes_session import router as session

    mounted = [
        Mounted(route, prefix)
        for router, prefix in (
            (dictation, "/api"),
            (admin, "/api"),
            (telemetry_router, "/api"),
            (jurisdictions, "/api"),
            (session, "/api"),
            (recording, ""),
        )
        for route in router.routes
        if isinstance(route, APIRoute)
    ]
    assert len(mounted) > 80  # not vacuous
    return mounted


def _keys(route: Mounted) -> set[tuple[str, str]]:
    return {(method, route.path) for method in route.methods if method != "HEAD"}


def _names(calls) -> set[str]:
    return {getattr(call, "__qualname__", "") for call in calls}


def test_every_route_authenticates_the_user_with_the_benchnotes_token_or_is_named_here(routes):
    unauthenticated = []
    for route in routes:
        calls = _tree(route)
        if get_token_auth_user in calls:
            continue
        keys = _keys(route)
        if keys <= OPEN or any(route.path.startswith(prefix) for prefix in API_KEY_PREFIXES):
            continue
        # A machine caller's API key, by the dependency that checks it.
        if "get_caller" in _names(calls):
            continue
        unauthenticated.append(sorted(keys))
    assert unauthenticated == [], f"routes with no authentication, not named in OPEN: {unauthenticated}"


def test_every_route_that_authenticates_a_user_needs_a_context_unless_it_is_a_no_context_route(routes):
    contexts_required = {dependencies_dictation.get_current_user, dependencies_recording.get_current_user}
    lenient = {dependencies_dictation.get_signed_in_user}
    for route in routes:
        calls = _tree(route)
        if get_token_auth_user not in calls:
            continue
        keys = _keys(route)
        if keys <= NO_CONTEXT:
            assert any(call in lenient for call in calls), f"{sorted(keys)} should be a no-context route"
            continue
        assert any(call in contexts_required for call in calls), (
            f"{sorted(keys)} authenticates a user but does not need a context, and is not in NO_CONTEXT"
        )


def test_the_named_routes_exist(routes):
    paths = {key for route in routes for key in _keys(route)}
    assert NO_CONTEXT <= paths
    assert ("POST", "/api/auth/session") in paths
