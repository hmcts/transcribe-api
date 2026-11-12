"""The permission matrix, generated from the application (DIAAT-376, DIAAT-389, DIAAT-403).

Nothing here lists routes or who may call them: the routes come from the FastAPI application, the
permission each declares comes from its ``require_permission`` dependency, and who holds it comes
from the contract's role map (tests/unit/test_permissions.py has the contract's copy). For every
route and every kind of caller:

* the route is allowed exactly when the permissions of the caller's active role, and of nothing
  else they hold, include one the route asks for (a SystemAdministrator's system ones only while
  acting as SystemAdministrator);
* otherwise it is refused with a 403, ``context_required`` with no context and
  ``permission_denied`` in one, before the handler runs (over HTTP, on the real application).

Also: a route with no declared permission (or exemption) fails the suite, the exemption list is
explicit and small, and nobody holds the ``*.any.*`` permissions. Read the printed matrix with
``pytest tests/unit/test_permission_matrix.py -k matrix_is_printed -s``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import fastapi.routing as fastapi_routing
import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.unit.test_permissions import CONTRACT_PERMISSIONS, CONTRACT_ROLE_MAP, CONTRACT_SYSADMIN
from transcribe_api.domain.auth import authorisation, dependencies_dictation
from transcribe_api.domain.auth import permissions as p
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.auth_models_dictation import AuthenticatedUser as DictationUser
from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser as RecordingUser
from transcribe_api.domain.auth.authorisation import (
    EXEMPT_PUBLIC,
    EXEMPT_SIGN_IN,
    EXEMPT_SIGNED_IN,
    EXEMPT_SIGNED_URL,
    declared_exemption,
    declared_permissions,
)
from transcribe_api.domain.models_dictation import User as DbUser

# ---------------------------------------------------------------------------
# The routes of the application
# ---------------------------------------------------------------------------

# No permission, on purpose: the whole list. Each needs a reason, and a new entry is a decision.
EXEMPT_ROUTES: dict[tuple[str, str], str] = {
    # health and version: no authentication
    ("GET", "/health"): EXEMPT_PUBLIC,
    ("GET", "/api/health"): EXEMPT_PUBLIC,
    ("GET", "/api/healthcheck"): EXEMPT_PUBLIC,
    ("GET", "/api/health/dependencies"): EXEMPT_PUBLIC,
    ("GET", "/api/v1/health"): EXEMPT_PUBLIC,
    ("GET", "/api/v1/version"): EXEMPT_PUBLIC,
    # sign-in: authenticates by the Entra ID token (or, locally, a persona)
    ("POST", "/api/auth/session"): EXEMPT_SIGN_IN,
    # any signed-in user, with or without a context or a role (contract section 3)
    ("GET", "/api/me/permissions"): EXEMPT_SIGNED_IN,
    ("PUT", "/api/me/context"): EXEMPT_SIGNED_IN,
    ("DELETE", "/api/me/jurisdictions/{code}"): EXEMPT_SIGNED_IN,
    ("GET", "/api/jurisdictions/public"): EXEMPT_SIGNED_IN,
    ("GET", "/api/flags"): EXEMPT_SIGNED_IN,
    # access requests (DIAAT-396): a user with no role asks for one (contract section 3); deciding them is access.approve
    ("POST", "/api/access-requests"): EXEMPT_SIGNED_IN,
    ("GET", "/api/access-requests/mine"): EXEMPT_SIGNED_IN,
    ("POST", "/api/access-requests/{request_id}/withdraw"): EXEMPT_SIGNED_IN,
    # a signed, expiring URL (Azure Speech fetches it without our token; 404 unless the local backend is on)
    ("GET", "/api/v1/local-audio/{blob_name}"): EXEMPT_SIGNED_URL,
}
# Routes that are not API routes (no dependencies at all): FastAPI's own OpenAPI document.
PLAIN_ROUTES = {"/api/openapi.json"}


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    dependant: object

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.path)

    def calls(self) -> list:
        found: list = []

        def walk(dependant) -> None:
            for dependency in dependant.dependencies:
                found.append(dependency.call)
                walk(dependency)

        walk(self.dependant)
        return found

    def permission_sets(self) -> set[frozenset[str]]:
        return {perms for call in self.calls() if (perms := declared_permissions(call)) is not None}

    def exemptions(self) -> set[str]:
        return {kind for call in self.calls() if (kind := declared_exemption(call)) is not None}


def routes_of(app: FastAPI) -> tuple[list[Route], list[str]]:
    """Every route the application serves, with its method, full path and dependency tree, and the
    paths of the plain (non-API) routes. FastAPI keeps each included router as a branch (it does not
    copy its routes): ``effective_route_contexts`` flattens them with their prefixes."""
    routes: list[Route] = []
    plain: list[str] = []
    for route in app.routes:
        if isinstance(route, fastapi_routing._IncludedRouter):
            flat = [(c.path_format, c.methods, c.dependant) for c in route.effective_route_contexts()]
        elif isinstance(route, APIRoute):
            flat = [(route.path, route.methods, route.dependant)]
        else:
            plain.append(getattr(route, "path", repr(route)))
            continue
        for path, methods, dependant in flat:
            routes += [Route(m, path, dependant) for m in sorted(methods - {"HEAD"})]
    return routes, plain


def user_dependencies(route: Route) -> list:
    """What resolves the caller for this route: what its permission (or signed-in) dependency depends on.
    Read off the application, so a test can stand in for it whatever was imported or reloaded when."""
    found: list = []

    def walk(dependant) -> None:
        for dependency in dependant.dependencies:
            if declared_permissions(dependency.call) is not None or declared_exemption(dependency.call) == EXEMPT_SIGNED_IN:
                found.extend(sub.call for sub in dependency.dependencies)
            walk(dependency)

    walk(route.dependant)
    return found


def sign_in_as(app: FastAPI, routes: list[Route], users: dict[str, object]) -> None:
    """Make every route see ``users[area]`` as the caller (the auth dependencies replaced, as the tests of the
    routes do). Undo with ``app.dependency_overrides.clear()``."""
    for route in routes:
        area = area_of(route)
        for call in user_dependencies(route):
            app.dependency_overrides[call] = lambda area=area: users[area]


def undeclared(routes: list[Route]) -> list[str]:
    """What is wrong with how the routes declare who may call them: none, or more than one,
    declaration (a permission or an exemption)."""
    problems = []
    for route in routes:
        declared = len(route.permission_sets()) + len(route.exemptions())
        if declared != 1:
            problems.append(f"{route.method} {route.path}: {declared} declarations (needs exactly one)")
    return problems


@pytest.fixture(scope="module")
def app() -> FastAPI:
    from transcribe_api.api.app import create_app

    return create_app()


@pytest.fixture(scope="module")
def routes(app) -> list[Route]:
    found, plain = routes_of(app)
    assert set(plain) == PLAIN_ROUTES, f"unexpected non-API routes: {sorted(set(plain) - PLAIN_ROUTES)}"
    assert len(found) > 80  # not vacuous
    return found


def guarded(routes: list[Route]) -> list[Route]:
    return [r for r in routes if r.key not in EXEMPT_ROUTES]


# ---------------------------------------------------------------------------
# Deny by default: no route without a declaration, and the exemptions are explicit
# ---------------------------------------------------------------------------


def test_every_route_declares_exactly_one_permission_or_exemption(routes):
    assert undeclared(routes) == []


# ---------------------------------------------------------------------------
# The declared permission of every route, pinned (review mutations M21 and M33)
# ---------------------------------------------------------------------------

SNAPSHOT = Path(__file__).with_name("permission_matrix_snapshot.json")


def declared_matrix(routes: list[Route]) -> dict[str, list[str]]:
    """``"METHOD /path"`` -> the permissions the route asks for (any one of them), or ``["exempt:<kind>"]``."""
    matrix: dict[str, list[str]] = {}
    for route in routes:
        (permissions,) = route.permission_sets() or [frozenset()]
        matrix[f"{route.method} {route.path}"] = (
            sorted(permissions) if permissions else [f"exempt:{kind}" for kind in sorted(route.exemptions())]
        )
    return dict(sorted(matrix.items()))


def test_the_declared_permission_of_every_route_is_the_committed_snapshot(routes):
    """The generated test below trusts what each route declares, so a route quietly weakened (re-declared
    with a permission more roles hold, or an exemption) would pass it. This pins every declaration: changing
    one is a visible diff of ``permission_matrix_snapshot.json`` to review, not a silent edit.

    A deliberate change: ``UPDATE_PERMISSION_SNAPSHOT=1 pytest tests/unit/test_permission_matrix.py -k committed_snapshot``
    rewrites the file; commit it with the change and say why in the message."""
    actual = declared_matrix(routes)
    if os.getenv("UPDATE_PERMISSION_SNAPSHOT"):
        SNAPSHOT.write_text(json.dumps(actual, indent=2) + "\n")
    committed = json.loads(SNAPSHOT.read_text())
    added = sorted(set(actual) - set(committed))
    removed = sorted(set(committed) - set(actual))
    changed = sorted(f"{key}: {committed[key]} -> {actual[key]}" for key in set(actual) & set(committed) if actual[key] != committed[key])
    assert (added, removed, changed) == ([], [], []), (
        "the routes' declared permissions differ from tests/unit/permission_matrix_snapshot.json: "
        "review, then regenerate it with UPDATE_PERMISSION_SNAPSHOT=1"
    )


def test_a_weakened_declaration_is_what_the_snapshot_catches(routes):
    """The mutation the review ran (``POST /api/user/reset-onboarding``: app.access -> document_content.read, and
    ``GET /api/templates``: document_content.read -> app.access) changes the matrix this test compares."""
    committed = json.loads(SNAPSHOT.read_text())
    assert committed["POST /api/user/reset-onboarding"] == ["app.access"]
    assert committed["GET /api/templates"] == ["document_content.read"]
    weakened = dict(committed) | {"GET /api/templates": ["app.access"]}
    assert weakened != declared_matrix(routes)


def test_a_route_without_a_declaration_fails_the_check():
    """The check above would catch the next route that forgets: shown on a made-up application."""
    require = dependencies_dictation.require_permission
    app = FastAPI()

    @app.get("/declared", dependencies=[Depends(require("dictation.use"))])
    async def declared() -> None: ...

    @app.get("/forgotten")
    async def forgotten() -> None: ...

    @app.get("/only-signed-in", dependencies=[Depends(dependencies_dictation.get_current_user)])
    async def only_signed_in() -> None: ...

    @app.get("/two", dependencies=[Depends(require("dictation.use")), Depends(require("recording.own"))])
    async def two() -> None: ...

    @app.get("/exempt", dependencies=[Depends(authorisation.public_endpoint)])
    async def exempt() -> None: ...

    found, _ = routes_of(app)
    assert sorted(p_.split(":")[0] for p_ in undeclared(found)) == ["GET /forgotten", "GET /only-signed-in", "GET /two"]


def test_the_exemptions_are_explicit_and_small(routes):
    exempt = {r.key: next(iter(r.exemptions())) for r in routes if r.exemptions()}
    assert exempt == EXEMPT_ROUTES
    assert len(EXEMPT_ROUTES) <= 16
    # An exempt route carries no permission as well.
    assert all(not r.permission_sets() for r in routes if r.key in EXEMPT_ROUTES)


def test_every_declared_permission_is_a_listed_one_that_somebody_holds(routes):
    held_by_someone = set(p.SYSTEM_ADMIN_PERMISSIONS).union(*p.ROLE_PERMISSIONS.values())
    for route in guarded(routes):
        (wanted,) = route.permission_sets()
        assert wanted <= set(CONTRACT_PERMISSIONS), route.key
        # A route nobody can call (the *.any.* permissions are granted to no role) is a mistake.
        assert wanted & held_by_someone, f"{route.key} asks for {sorted(wanted)}, which no role holds"


def test_the_contracts_permissions_exist_and_the_any_permissions_are_granted_to_nobody(routes):
    assert set(CONTRACT_PERMISSIONS) <= set(p.PERMISSIONS)
    anyone = {"recording.any.read", "recording.any.edit", "recording.any.delete", "dictation.any.read"}
    for role, held in CONTRACT_ROLE_MAP.items():
        assert not anyone & set(held), role
    assert not anyone & set(CONTRACT_SYSADMIN)
    for persona in PERSONAS.values():
        assert not anyone & set(persona.permissions)
    for route in routes:
        assert not anyone & set().union(*route.permission_sets()), route.key


def test_system_administrator_is_in_no_role_list():
    assert "SystemAdministrator" not in p.STORED_ROLES
    assert "SystemAdministrator" not in p.ROLE_PERMISSIONS
    assert "SystemAdministrator" not in p.REQUESTABLE_ROLES
    for role in (*p.STORED_ROLES, None):
        for sysadmin in (False, True):
            assert "SystemAdministrator" not in p.assignable_roles(role, sysadmin)
    assert "Normal" not in p.STORED_ROLES


# ---------------------------------------------------------------------------
# The callers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Persona:
    name: str
    ctx: Context | None
    sysadmin: bool
    permissions: frozenset[str]  # from the contract's table, not from the code under test


def _persona(name: str, ctx: Context | None, sysadmin: bool) -> Persona:
    if ctx is None:
        held: set[str] = set()
    elif ctx.role == "SystemAdministrator":
        held = set(CONTRACT_SYSADMIN) if sysadmin else set()
    else:
        held = set(CONTRACT_ROLE_MAP[ctx.role])
    return Persona(name, ctx, sysadmin, frozenset(held))


_ROLE_CONTEXTS = {
    "Judge": Context("IAC", "Judge"),
    "LegalTextManager": Context("IAC", "LegalTextManager"),
    "JurisdictionAdministrator": Context("IAC", "JurisdictionAdministrator"),
    "JurisdictionSuperAdministrator": Context(None, "JurisdictionSuperAdministrator"),
}
SYSTEM_ADMIN_CONTEXT = Context(None, "SystemAdministrator")
PERSONAS: dict[str, Persona] = {
    **{role: _persona(role, ctx, False) for role, ctx in _ROLE_CONTEXTS.items()},
    "SystemAdministrator": _persona("SystemAdministrator", SYSTEM_ADMIN_CONTEXT, True),
    # A token cannot carry it (app_token.verify), and the check does not rely on that.
    "SystemAdministrator context without the Entra role": _persona(
        "SystemAdministrator context without the Entra role", SYSTEM_ADMIN_CONTEXT, False
    ),
    "sysadmin with no context": _persona("sysadmin with no context", None, True),
    "no-role": _persona("no-role", None, False),
    **{f"sysadmin acting as {role}": _persona(f"sysadmin acting as {role}", ctx, True) for role, ctx in _ROLE_CONTEXTS.items()},
}

# A user who holds several roles acts in one at a time: the matrix runs for each context. The second
# holds every role and the Entra SystemAdministrator role (DIAAT-403: what they see as a Judge is a Judge's).
TWO_ROLES = (Context("ET", "Judge"), Context("IAC", "JurisdictionAdministrator"))
EVERY_ROLE = (*_ROLE_CONTEXTS.values(), SYSTEM_ADMIN_CONTEXT)
HELD_BY = {"two-roles": TWO_ROLES, "every-role": EVERY_ROLE}
for _ctx in TWO_ROLES:
    PERSONAS[f"two-roles acting as {_ctx.role}@{_ctx.jurisdiction}"] = _persona(
        f"two-roles acting as {_ctx.role}@{_ctx.jurisdiction}", _ctx, False
    )
for _ctx in EVERY_ROLE:
    PERSONAS[f"every-role acting as {_ctx.role}@{_ctx.jurisdiction}"] = _persona(
        f"every-role acting as {_ctx.role}@{_ctx.jurisdiction}", _ctx, True
    )


def user_for(persona: Persona, area: str):
    """The authenticated user the auth dependency would hand the route for this caller."""
    cls = DictationUser if area == "dictation" else RecordingUser
    group = persona.name.split(" ", 1)[0]
    held = HELD_BY.get(group) or ((persona.ctx,) if persona.ctx else ())
    db_user = DbUser(id=uuid4(), email=f"{persona.name}@example.test", azure_user_id=str(uuid4()))
    return cls(db_user=db_user, ctx=persona.ctx, sysadmin=persona.sysadmin, assignments=tuple(held))


def area_of(route: Route) -> str:
    """Which area's authenticated user the route uses (the recording area is /api/v1)."""
    return "recording" if route.path.startswith("/api/v1") else "dictation"


def allowed(persona: Persona, route: Route) -> bool:
    (wanted,) = route.permission_sets()
    return bool(persona.permissions & wanted)


@pytest.fixture
def audit_rows(monkeypatch):
    """The ACCESS_DENIED rows written (no database here)."""
    rows: list = []
    monkeypatch.setattr(authorisation, "write_audit_event", rows.append)
    monkeypatch.setattr(authorisation, "record_audit", lambda *a, **k: rows.append((a, k)))
    return rows


class _Request:
    method = "GET"
    client = None

    class url:
        path = "/probe"


def call_dependency(route: Route, user):
    """Run the route's own permission dependency (the one it declares) for ``user``."""
    (dependency,) = {c for c in route.calls() if declared_permissions(c) is not None}
    return asyncio.run(dependency(request=_Request(), current_user=user))


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


def test_each_route_is_allowed_exactly_to_the_callers_whose_permissions_grant_it(routes, audit_rows):
    wrong = []
    for route in guarded(routes):
        for persona in PERSONAS.values():
            user = user_for(persona, area_of(route))
            try:
                outcome = call_dependency(route, user)
            except Exception as exc:
                got_through = False
                code = getattr(exc, "detail", {}).get("code") if getattr(exc, "status_code", None) == 403 else repr(exc)
            else:
                got_through, code = True, None
                if outcome is not user:
                    wrong.append(f"{route.method} {route.path} as {persona.name}: did not return the authenticated user")
            expected = allowed(persona, route)
            if got_through != expected:
                wrong.append(f"{route.method} {route.path} as {persona.name}: allowed={got_through}, contract says {expected}")
            elif not expected:
                want_code = "context_required" if persona.ctx is None else "permission_denied"
                if code != want_code:
                    wrong.append(f"{route.method} {route.path} as {persona.name}: refused with {code}, expected {want_code}")
    assert wrong == []


def test_a_denial_in_a_context_is_audited_with_the_context_and_one_with_no_context_is_not(routes, audit_rows):
    route = next(r for r in guarded(routes) if r.path == "/api/transcriptions-metadata")
    judge_less = user_for(PERSONAS["LegalTextManager"], "dictation")
    with pytest.raises(Exception) as refused:
        call_dependency(route, judge_less)
    assert refused.value.status_code == 403
    (event,) = audit_rows
    assert event.event_type == "ACCESS_DENIED"
    assert event.held_roles == ["LegalTextManager"] and event.required_roles == ["dictation.use"]
    assert event.detail == {"reason": "permission_denied"}

    audit_rows.clear()
    with pytest.raises(Exception) as no_context:
        call_dependency(route, user_for(PERSONAS["sysadmin with no context"], "dictation"))
    assert no_context.value.detail["code"] == "context_required"
    assert audit_rows == []


@pytest.mark.parametrize("persona_name", list(PERSONAS))
def test_refused_callers_never_reach_a_handler_over_http(app, routes, audit_rows, persona_name):
    """The same matrix on the real application: a caller the map does not allow gets the 403 of
    ``require_permission`` for every route, and with the handler never run (not even a body or a
    path parameter is looked at). The caller is the one the auth dependency would resolve."""
    persona = PERSONAS[persona_name]
    sign_in_as(app, routes, {area: user_for(persona, area) for area in ("dictation", "recording")})
    client = TestClient(app, raise_server_exceptions=False)
    try:
        wrong = []
        for route in guarded(routes):
            if allowed(persona, route):
                continue
            path = re.sub(r"\{\w+(:\w+)?\}", lambda _m: str(uuid4()), route.path)
            response = client.request(route.method, path)
            want_code = "context_required" if persona.ctx is None else "permission_denied"
            detail = response.json().get("detail") if response.headers.get("content-type", "").startswith("application/json") else None
            if response.status_code != 403 or not isinstance(detail, dict) or detail.get("code") != want_code:
                wrong.append(f"{route.method} {route.path}: {response.status_code} {detail}")
        assert wrong == []
    finally:
        app.dependency_overrides.clear()


def test_only_a_judge_reaches_the_dictation_and_recording_routes(routes):
    """DIAAT-376's first acceptance criterion, read off the generated matrix: the routes of
    the dictation product and of hearing recordings are reached by a Judge acting as one, and
    by no other caller: not another role, and not a SystemAdministrator whatever else they act as."""
    own_data = {"dictation.use", "recording.upload", "recording.own", "documents.export"}
    for route in guarded(routes):
        (wanted,) = route.permission_sets()
        if not wanted & own_data:
            continue
        for persona in PERSONAS.values():
            judge_acting = persona.ctx is not None and persona.ctx.role == "Judge"
            assert allowed(persona, route) is judge_acting, (route.key, persona.name)


JUDGE_ONLY_PREFIXES = (
    "/api/v1/",  # hearing recordings
    "/api/transcriptions",  # dictation records and everything under them
    "/api/live-transcription",
    "/api/live-draft",
    "/api/get-speech-token",
    "/api/get-upload-url",
    "/api/upload-audio",
    "/api/generate-",
    "/api/download",
    "/api/langfuse",
)


def test_the_dictation_and_recording_routes_are_a_judges_alone_by_where_they_live(routes):
    """Independent of what each declares: every route under these paths is allowed to a Judge acting
    as one and to no other caller (not any other role, nor a SystemAdministrator, in any context)."""
    judge_routes = [r for r in guarded(routes) if r.path.startswith(JUDGE_ONLY_PREFIXES)]
    assert len(judge_routes) >= 40  # not vacuous
    for route in judge_routes:
        for persona in PERSONAS.values():
            judge_acting = persona.ctx is not None and persona.ctx.role == "Judge"
            assert allowed(persona, route) is judge_acting, (route.key, persona.name)


def test_legal_text_managers_reach_only_legal_text_templates_and_the_admin_landing_page(routes):
    ltm = PERSONAS["LegalTextManager"]
    reached = {r.key for r in guarded(routes) if allowed(ltm, r)}
    for method, path in reached:
        assert path.startswith("/api/admin/document-content") or path in {
            "/api/admin/me",  # the admin landing page
            "/api/templates",  # the template list
            "/api/document-content",  # read-only document content
            "/api/user",  # the app's own user routes (app.access)
            "/api/users/me",
            "/api/user/profile",
            "/api/user/onboarding-status",
            "/api/user/complete-onboarding",
            "/api/user/reset-onboarding",
            "/api/telemetry",
            "/api/jurisdictions/current",
        }, (method, path)


def test_a_system_administrator_has_only_the_system_routes_and_the_admin_landing_page(routes):
    system = PERSONAS["SystemAdministrator"]
    reached = {r.key for r in guarded(routes) if allowed(system, r)}
    assert reached == {
        ("POST", "/api/security/alert-test"),  # security.alert_test
        ("GET", "/api/healthcheck/azure-storage"),  # system.settings
        ("GET", "/api/admin/me"),  # admin.access
        ("GET", "/api/admin/roles"),  # jurisdiction_*_admin.assign: the roles they may assign
        ("GET", "/api/admin/users"),  # jurisdiction_super_admin.assign: to find who to promote
        ("POST", "/api/admin/assignments"),  # jurisdiction_*_admin.assign: the rules decide which role
        ("DELETE", "/api/admin/assignments/{assignment_id}"),
        ("GET", "/api/admin/feature-flags"),  # feature_flags.manage (DIAAT-392)
        ("PUT", "/api/admin/feature-flags/{name}"),
    }


def test_the_matrix_is_printed(routes, capsys):
    """For reading (``-s``): one line per route, then the callers allowed on it."""
    names = list(PERSONAS)
    with capsys.disabled():
        print()
        for route in guarded(routes):
            (wanted,) = route.permission_sets()
            who = [n for n in names if allowed(PERSONAS[n], route) and not n.startswith(("sysadmin ", "two-roles", "every-role"))]
            print(f"{route.method:6} {route.path:75} {'|'.join(sorted(wanted)):45} {', '.join(who)}")


# DIAAT-383, 391, 392, 394, 395, 396: what each new route of Phase 4 asks for, written out from the
# contract (section 3), so that a route cannot drift to another permission unnoticed.
ADMIN_ROUTE_PERMISSIONS: dict[tuple[str, str], set[str]] = {
    ("GET", "/api/admin/users"): {"users.manage", "jurisdiction_super_admin.assign"},
    ("POST", "/api/admin/assignments"): {"users.manage", "jurisdiction_admin.assign", "jurisdiction_super_admin.assign"},
    ("DELETE", "/api/admin/assignments/{assignment_id}"): {
        "users.manage",
        "jurisdiction_admin.assign",
        "jurisdiction_super_admin.assign",
    },
    ("POST", "/api/admin/users/{user_id}/deactivate"): {"users.manage"},
    ("POST", "/api/admin/users/{user_id}/reactivate"): {"users.manage"},
    ("GET", "/api/admin/role-history"): {"role_audit.read"},
    ("GET", "/api/admin/access-requests"): {"access.approve"},
    ("POST", "/api/admin/access-requests/{request_id}/approve"): {"access.approve"},
    ("POST", "/api/admin/access-requests/{request_id}/reject"): {"access.approve"},
    ("GET", "/api/admin/feature-flags"): {"feature_flags.manage"},
    ("PUT", "/api/admin/feature-flags/{name}"): {"feature_flags.manage"},
    ("GET", "/api/admin/jurisdictions/{code}/wording"): {"jurisdiction.wording.edit"},
    ("PUT", "/api/admin/jurisdictions/{code}/wording"): {"jurisdiction.wording.edit"},
    ("GET", "/api/v1/jobs/{job_id}/history"): {"recording.audit.read"},
}


def test_the_phase_4_routes_declare_the_permissions_the_contract_names(routes):
    by_key = {r.key: r for r in routes}
    for key, wanted in ADMIN_ROUTE_PERMISSIONS.items():
        assert key in by_key, f"{key} is not a route"
        assert by_key[key].permission_sets() == {frozenset(wanted)}, key


def test_the_removed_interim_routes_are_gone(routes):
    keys = {r.key for r in routes}
    assert ("GET", "/api/admin/users/{user_id}") not in keys
    assert not [k for k in keys if k[1].endswith("/members")]


def test_a_system_administrator_is_refused_the_approvals_and_every_jurisdiction_route(routes):
    """DIAAT-389 and DIAAT-396: no access.approve, no jurisdiction management, no wording."""
    system = PERSONAS["SystemAdministrator"]
    for key in (
        ("GET", "/api/admin/access-requests"),
        ("POST", "/api/admin/access-requests/{request_id}/approve"),
        ("POST", "/api/admin/access-requests/{request_id}/reject"),
        ("GET", "/api/admin/jurisdictions"),
        ("PUT", "/api/admin/jurisdictions/{code}"),
        ("GET", "/api/admin/jurisdictions/{code}/wording"),
        ("PUT", "/api/admin/jurisdictions/{code}/wording"),
        ("GET", "/api/admin/role-history"),
        ("POST", "/api/admin/users/{user_id}/deactivate"),
    ):
        route = next(r for r in routes if r.key == key)
        assert not allowed(system, route), key


def test_a_user_holding_every_role_reaches_in_each_context_exactly_what_that_role_alone_reaches(routes):
    """DIAAT-403: the effective permissions are the active context's, never the union of what the user
    holds. Someone holding every role and the Entra SystemAdministrator role, acting as Judge, reaches the
    routes a plain Judge reaches and nothing more (no admin, user, role or feature flag route)."""
    for ctx in EVERY_ROLE:
        alone = PERSONAS["SystemAdministrator" if ctx.role == "SystemAdministrator" else ctx.role]
        holder = PERSONAS[f"every-role acting as {ctx.role}@{ctx.jurisdiction}"]
        assert {r.key for r in guarded(routes) if allowed(holder, r)} == {r.key for r in guarded(routes) if allowed(alone, r)}
    judge = PERSONAS["every-role acting as Judge@IAC"]
    assert not [r.key for r in guarded(routes) if r.path.startswith("/api/admin") and allowed(judge, r)]


def test_the_system_permissions_never_leak_into_another_context(routes):
    system_only = {"feature_flags.manage", "system.settings", "security.alert_test", "jurisdiction_super_admin.assign"}
    for persona in PERSONAS.values():
        acting_as_system = persona.ctx == SYSTEM_ADMIN_CONTEXT and persona.sysadmin
        assert bool(persona.permissions & system_only) is acting_as_system, persona.name
