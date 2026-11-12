"""``require_permission``, the jurisdiction scope and the exemption markers (DIAAT-376;
domain/auth/authorisation.py). Which routes declare what is test_permission_matrix.py."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from tests.unit.jurisdiction_fixtures import add_jurisdiction, add_user
from transcribe_api.domain.auth import authorisation, dependencies_dictation
from transcribe_api.domain.auth import context as auth_context
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.auth_models_dictation import AuthenticatedUser
from transcribe_api.domain.auth.authorisation import (
    assert_jurisdiction_in_scope,
    build_require_permission,
    declared_exemption,
    declared_permissions,
    ensure_permission,
    jurisdiction_scope,
    permissions_of,
)
from transcribe_api.domain.models_dictation import AuditLog, User


def require_permission(*permissions):
    """The dictation area's, looked up now (another test reloads that module)."""
    return dependencies_dictation.require_permission(*permissions)


def _user(role: str | None = "Judge", jurisdiction: str | None = "IAC", *, sysadmin: bool = False) -> AuthenticatedUser:
    ctx = Context(jurisdiction, role) if role else None
    return AuthenticatedUser(db_user=User(id=uuid4(), email="u@example.test", azure_user_id="oid"), ctx=ctx, sysadmin=sysadmin)


class _Request:
    method = "PUT"
    client = None

    class url:
        path = "/probe/1"


@pytest.fixture
def audit_rows(monkeypatch):
    rows: list = []
    monkeypatch.setattr(authorisation, "write_audit_event", rows.append)
    monkeypatch.setattr(authorisation, "record_audit", lambda *a, **k: rows.append((a, k)))
    return rows


def _run(permission_dependency, user):
    return asyncio.run(permission_dependency(request=_Request(), current_user=user))


# --- building it ---------------------------------------------------------------------------


def test_an_unknown_permission_or_none_is_refused_when_the_route_is_defined():
    with pytest.raises(ValueError, match="unknown permission"):
        require_permission("dictation.usee")
    with pytest.raises(ValueError, match="at least one"):
        require_permission()


def test_the_same_permissions_are_one_dependency_whatever_the_order_so_fastapi_resolves_it_once():
    assert require_permission("dictation.use") is require_permission("dictation.use")
    assert require_permission("users.manage", "app.access") is require_permission("app.access", "users.manage")
    assert require_permission("dictation.use") is not require_permission("recording.own")


def test_the_dependency_says_what_it_asks_for_and_other_callables_say_nothing():
    assert declared_permissions(require_permission("users.manage", "app.access")) == {"users.manage", "app.access"}
    assert declared_permissions(dependencies_dictation.get_current_user) is None
    assert declared_exemption(require_permission("app.access")) is None


def test_each_area_has_its_own_user_dependency_and_so_its_own_callable():
    other = build_require_permission(lambda: None)
    assert other("dictation.use") is not require_permission("dictation.use")


# --- the decision --------------------------------------------------------------------------


def test_a_caller_holding_one_of_the_permissions_gets_the_user_back(audit_rows):
    user = _user("JurisdictionAdministrator")
    assert _run(require_permission("recording.own", "users.manage"), user) is user
    assert audit_rows == []


def test_a_caller_holding_none_is_refused_and_audited_with_what_they_hold_and_what_was_asked(audit_rows):
    caller = _user("LegalTextManager")
    with pytest.raises(Exception) as refused:
        _run(require_permission("recording.own", "dictation.use"), caller)
    assert refused.value.status_code == 403
    assert refused.value.detail["code"] == "permission_denied"
    (event,) = audit_rows
    assert (event.event_type, event.user_id, event.resource) == ("ACCESS_DENIED", str(caller.id), "PUT /probe/1")
    assert event.held_roles == ["LegalTextManager"]
    assert event.required_roles == ["dictation.use", "recording.own"]
    assert event.detail == {"reason": "permission_denied"}


def test_no_context_is_context_required_for_everyone(audit_rows):
    for permission in ("dictation.use", "jurisdictions.manage", "feature_flags.manage"):
        with pytest.raises(Exception) as refused:
            _run(require_permission(permission), _user(None, sysadmin=True))
        assert refused.value.detail["code"] == "context_required"
    with pytest.raises(Exception) as nobody:
        _run(require_permission("app.access"), _user(None))
    assert nobody.value.detail["code"] == "context_required"
    assert audit_rows == []  # a missing context is not a refusal of a role


def test_the_system_administrators_permissions_apply_only_in_its_own_context(audit_rows):
    """DIAAT-403: a SystemAdministrator acting as a Judge is a Judge, and nothing more."""
    sysadmin_judge = _user("Judge", sysadmin=True)
    assert _run(require_permission("dictation.use"), sysadmin_judge) is sysadmin_judge
    for permission in ("security.alert_test", "feature_flags.manage", "admin.access", "jurisdiction_super_admin.assign"):
        with pytest.raises(Exception) as refused:
            _run(require_permission(permission), sysadmin_judge)
        assert refused.value.detail["code"] == "permission_denied"
    assert audit_rows[-1].held_roles == ["Judge"]

    system = _user("SystemAdministrator", None, sysadmin=True)
    assert _run(require_permission("security.alert_test"), system) is system
    with pytest.raises(Exception) as refused:
        _run(require_permission("dictation.use"), system)
    assert refused.value.detail["code"] == "permission_denied"
    forged = _user("SystemAdministrator", None, sysadmin=False)
    with pytest.raises(Exception) as refused:
        _run(require_permission("security.alert_test"), forged)
    assert refused.value.detail["code"] == "permission_denied"


def test_permissions_are_those_of_the_active_role_only():
    """A user holding two roles has, at any time, the permissions of the one they act as."""
    two = [Context("ET", "Judge"), Context("IAC", "JurisdictionAdministrator")]
    for acting in two:
        user = AuthenticatedUser(
            db_user=User(id=uuid4(), email="u@example.test", azure_user_id="oid"), ctx=acting, assignments=tuple(two)
        )
        held = set(permissions_of(user))
        assert ("dictation.use" in held) is (acting.role == "Judge")
        assert ("users.manage" in held) is (acting.role == "JurisdictionAdministrator")


# --- jurisdiction scope --------------------------------------------------------------------


def test_the_scope_of_a_jurisdiction_permission_is_the_active_jurisdiction_or_every_one_for_a_super_administrator():
    assert jurisdiction_scope(_user("JurisdictionAdministrator", "IAC")) == {"IAC"}
    assert jurisdiction_scope(_user("LegalTextManager", "et")) == {"ET"}
    assert jurisdiction_scope(_user("JurisdictionSuperAdministrator", None)) is None
    assert jurisdiction_scope(_user(None, sysadmin=True)) == frozenset()


def test_a_jurisdiction_administrator_is_in_scope_only_in_the_jurisdiction_they_act_in(audit_rows):
    admin = _user("JurisdictionAdministrator", "IAC")
    assert_jurisdiction_in_scope(admin, "IAC")
    assert_jurisdiction_in_scope(admin, " iac ")
    with pytest.raises(Exception) as refused:
        assert_jurisdiction_in_scope(admin, "ET")
    assert (refused.value.status_code, refused.value.detail["code"]) == (403, "jurisdiction_not_in_scope")
    (args, kwargs) = audit_rows[-1]
    assert args == ("ACCESS_DENIED",) and kwargs["outcome"] == "denied" and kwargs["detail"]["code"] == "ET"


def test_a_super_administrator_is_in_scope_everywhere_and_a_system_administrator_nowhere():
    super_admin = _user("JurisdictionSuperAdministrator", None)
    for code in ("IAC", "ET", "SSCS"):
        assert_jurisdiction_in_scope(super_admin, code)
    with pytest.raises(Exception) as refused:
        assert_jurisdiction_in_scope(_user(None, sysadmin=True), "IAC")
    assert refused.value.detail["code"] == "jurisdiction_not_in_scope"


def test_ensure_permission_is_the_second_check_of_a_route(audit_rows):
    ensure_permission(_user("JurisdictionSuperAdministrator", None), "jurisdiction.create")
    with pytest.raises(Exception) as refused:
        ensure_permission(_user("JurisdictionAdministrator"), "jurisdiction.create")
    assert (refused.value.status_code, refused.value.detail["code"]) == (403, "permission_denied")
    assert audit_rows[-1][1]["detail"] == {"reason": "permission_denied", "permission": "jurisdiction.create"}


# --- over HTTP, with a real request -----------------------------------------------------------


def _client(user) -> TestClient:
    app = FastAPI()

    @app.get("/gated")
    async def gated(current_user=Depends(require_permission("dictation.use"))) -> dict:  # noqa: B008
        return {"user": str(current_user.id)}

    @app.get("/open", dependencies=[Depends(authorisation.public_endpoint)])
    async def open_route() -> dict:
        return {"ok": True}

    app.dependency_overrides[dependencies_dictation.get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def test_the_error_body_is_the_apis_code_and_message_shape(audit_rows):
    response = _client(_user("LegalTextManager")).get("/gated")
    assert response.status_code == 403
    assert set(response.json()["detail"]) == {"code", "message"}
    assert _client(_user("Judge")).get("/gated").status_code == 200
    assert _client(_user("Judge")).get("/open").status_code == 200


@pytest.mark.real_jurisdictions
def test_a_denial_is_a_row_of_the_audit_log_stamped_with_the_active_context(jurisdiction_db, monkeypatch):
    """The real ACCESS_DENIED row: outcome denied, the context the caller acted in on it."""
    from transcribe_api.domain import audit
    from transcribe_api.domain.auth.context import AuditContext, set_audit_context

    iac = add_jurisdiction(jurisdiction_db, "IAC")
    caller = add_user(jurisdiction_db, "legal@example.test")
    user = AuthenticatedUser(db_user=caller, ctx=Context("IAC", "LegalTextManager"))
    monkeypatch.setattr(audit, "get_engine", lambda: jurisdiction_db)

    set_audit_context(AuditContext(jurisdiction_id=iac.id, role="LegalTextManager"))
    try:
        with pytest.raises(HTTPException) as refused:
            _run(require_permission("dictation.use"), user)
        assert refused.value.status_code == 403
    finally:
        set_audit_context(None)

    with Session(jurisdiction_db) as session:
        (row,) = session.exec(select(AuditLog)).all()
    assert (row.event_type, row.outcome, row.user_id) == ("ACCESS_DENIED", "denied", str(caller.id))
    assert (row.active_jurisdiction_id, row.active_role) == (iac.id, "LegalTextManager")
    assert (row.held_roles, row.required_roles) == (["LegalTextManager"], ["dictation.use"])
    assert auth_context.context_of(user) == Context("IAC", "LegalTextManager")
