"""Role, access request and feature flag changes are audited in the same transaction (review finding D2).

The audit row used to be written after the change was committed, in a transaction of its own and
fail-open, so a role change could be committed with no history row (the role history reads only
those rows). Now the row is added to the change's own transaction: if it cannot be written, the
change is not made. The tests inject an audit failure for each kind of change and check that nothing
is left of the change. (Non-authz audit, e.g. refusals, stays fail-open: ``test_audit*``.)
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import event
from sqlmodel import Session, select

from tests.unit import auth_fixtures as af
from tests.unit.admin_fixtures import ADMIN, JUDGE, LTM, SUPER, make_world
from transcribe_api.domain import assignments as assignment_service
from transcribe_api.domain.models_dictation import (
    AccessRequest,
    AuditLog,
    RoleAssignment,
    SystemFeatureFlag,
)

pytestmark = pytest.mark.real_jurisdictions


@pytest.fixture(autouse=True)
def no_email(monkeypatch):
    from transcribe_api.domain.notify import gov_notify

    monkeypatch.setattr(gov_notify, "_get_notifications_client", lambda: None)


@pytest.fixture
def world(jurisdiction_db, monkeypatch):
    yield make_world(jurisdiction_db, monkeypatch)
    af.unstub_entra()


@pytest.fixture
def cast(world):
    return type(
        "Cast",
        (),
        {
            "jadmin": world.person("Admin IAC", ("IAC", ADMIN)),
            "jsuper": world.person("Super", (None, SUPER)),
            "sysadmin": world.person("Sysadmin", sysadmin=True),
            "judge": world.person("Ivy Judge", ("IAC", JUDGE), ("ET", JUDGE)),
            "nobody": world.person("Nora Nobody"),
            "other": world.person("Olly Other", ("IAC", LTM)),
        },
    )


@contextmanager
def audit_fails(*event_types: str):
    """Every insert of an audit row of these types fails (a database error, the pod killed between statements)."""

    def boom(_mapper, _connection, target):
        if target.event_type in event_types:
            msg = "audit insert failed"
            raise RuntimeError(msg)

    event.listen(AuditLog, "before_insert", boom)
    try:
        yield
    finally:
        event.remove(AuditLog, "before_insert", boom)


def audit_rows(world, event_type: str) -> list[AuditLog]:
    return world.audit(event_type)


def test_an_assignment_whose_audit_row_cannot_be_written_is_not_made(world, cast):
    with audit_fails("assignment.added"):
        response = cast.jadmin.post(
            "/api/admin/assignments",
            role=ADMIN,
            jurisdiction="IAC",
            json={"user_id": str(cast.nobody.user_id), "jurisdiction": "IAC", "role": JUDGE},
        )
    assert response.status_code == 500
    assert world.assignments_of(cast.nobody) == set()
    assert audit_rows(world, "assignment.added") == []


def test_a_pending_assignment_whose_audit_row_cannot_be_written_is_not_made(world, cast):
    with audit_fails("assignment.added"):
        response = cast.jadmin.post(
            "/api/admin/assignments",
            role=ADMIN,
            jurisdiction="IAC",
            json={"email": "later@justice.gov.uk", "jurisdiction": "IAC", "role": JUDGE},
        )
    assert response.status_code == 500
    assert world.pending_for("later@justice.gov.uk") == set()


def test_a_role_removal_whose_audit_row_cannot_be_written_is_not_made(world, cast):
    with Session(world.engine) as session:
        row = session.exec(
            select(RoleAssignment).where(RoleAssignment.user_id == cast.other.user_id)
        ).one()
        row_id = row.id
    with audit_fails("assignment.removed"):
        response = cast.jadmin.delete(f"/api/admin/assignments/{row_id}", role=ADMIN, jurisdiction="IAC")
    assert response.status_code == 500
    assert world.assignments_of(cast.other) == {("IAC", LTM)}


@pytest.mark.parametrize(("failing", "start", "path"), [("user.deactivated", True, "deactivate"), ("user.reactivated", False, "reactivate")])
def test_a_deactivation_or_reactivation_whose_audit_row_cannot_be_written_is_not_made(world, cast, failing, start, path):
    if not start:
        assert cast.jadmin.post(f"/api/admin/users/{cast.other.user_id}/deactivate", role=ADMIN, jurisdiction="IAC").status_code == 204
    with audit_fails(failing):
        response = cast.jadmin.post(f"/api/admin/users/{cast.other.user_id}/{path}", role=ADMIN, jurisdiction="IAC")
    assert response.status_code == 500
    assert world.user(cast.other).is_active is start


def test_a_request_whose_audit_row_cannot_be_written_is_not_made(world, cast):
    with audit_fails("access_request.created"):
        response = cast.nobody.post("/api/access-requests", json={"jurisdiction": "IAC", "roles": [JUDGE]})
    assert response.status_code == 500
    with Session(world.engine) as session:
        assert session.exec(select(AccessRequest)).all() == []
    # The same request goes through when the audit works: nothing was left half-made (no stale lock, no row).
    assert cast.nobody.post("/api/access-requests", json={"jurisdiction": "IAC", "roles": [JUDGE]}).status_code == 201


def test_a_withdrawal_whose_audit_row_cannot_be_written_leaves_the_request_pending(world, cast):
    request_id = cast.nobody.post("/api/access-requests", json={"jurisdiction": "IAC", "roles": [JUDGE]}).json()["id"]
    with audit_fails("access_request.withdrawn"):
        response = cast.nobody.post(f"/api/access-requests/{request_id}/withdraw")
    assert response.status_code == 500
    assert cast.nobody.get("/api/access-requests/mine").json()[0]["status"] == "pending"


@pytest.mark.parametrize(
    ("decision", "failing"),
    [("approve", "access_request.approved"), ("reject", "access_request.rejected")],
)
def test_a_decision_whose_audit_row_cannot_be_written_leaves_the_request_pending_and_assigns_nothing(
    world, cast, decision, failing
):
    request_id = cast.nobody.post("/api/access-requests", json={"jurisdiction": "IAC", "roles": [JUDGE, LTM]}).json()["id"]
    with audit_fails(failing):
        response = cast.jadmin.post(f"/api/admin/access-requests/{request_id}/{decision}", role=ADMIN, jurisdiction="IAC", json={})
    assert response.status_code == 500
    assert cast.nobody.get("/api/access-requests/mine").json()[0]["status"] == "pending"
    assert world.assignments_of(cast.nobody) == set()
    # Nothing is stuck: the decision can be made.
    assert cast.jadmin.post(f"/api/admin/access-requests/{request_id}/{decision}", role=ADMIN, jurisdiction="IAC", json={}).status_code == 200
    assert len(audit_rows(world, failing)) == 2  # one history row per role


def test_the_second_of_two_audit_rows_of_a_decision_failing_undoes_the_whole_decision(world, cast):
    """An approval of two roles is two history rows: all or nothing, with the assignments."""
    request_id = cast.nobody.post("/api/access-requests", json={"jurisdiction": "IAC", "roles": [JUDGE, LTM]}).json()["id"]
    inserted = []

    def second_fails(_mapper, _connection, target):
        if target.event_type == "access_request.approved":
            inserted.append(target.event_type)
            if len(inserted) == 2:
                msg = "second audit insert failed"
                raise RuntimeError(msg)

    event.listen(AuditLog, "before_insert", second_fails)
    try:
        response = cast.jadmin.post(f"/api/admin/access-requests/{request_id}/approve", role=ADMIN, jurisdiction="IAC", json={})
    finally:
        event.remove(AuditLog, "before_insert", second_fails)
    assert response.status_code == 500
    assert world.assignments_of(cast.nobody) == set()
    assert audit_rows(world, "access_request.approved") == []
    assert cast.nobody.get("/api/access-requests/mine").json()[0]["status"] == "pending"


def test_leaving_a_jurisdiction_whose_audit_row_cannot_be_written_does_not_leave(world, cast):
    with audit_fails("membership.leave"):
        response = cast.judge.delete("/api/me/jurisdictions/ET")
    assert response.status_code == 500
    assert ("ET", JUDGE) in world.assignments_of(cast.judge)
    assert cast.judge.delete("/api/me/jurisdictions/ET").status_code == 200


def test_a_feature_flag_whose_audit_row_cannot_be_written_is_not_changed(world, cast, monkeypatch):
    from transcribe_api.domain import feature_flags

    SQLModelFlag = SystemFeatureFlag
    SQLModelFlag.__table__.create(world.engine, checkfirst=True)
    monkeypatch.setattr(feature_flags, "get_engine", lambda: world.engine)
    from transcribe_api.runtime import db_connection

    monkeypatch.setattr(db_connection, "get_engine", lambda: world.engine)
    with audit_fails("feature_flag.changed"):
        response = cast.sysadmin.put("/api/admin/feature-flags/dictation_enabled", json={"value": False})
    assert response.status_code == 500
    with Session(world.engine) as session:
        assert session.exec(select(SystemFeatureFlag)).all() == []
    assert cast.sysadmin.get("/api/admin/feature-flags").json()["flags"][0]["value"] is True
    ok = cast.sysadmin.put("/api/admin/feature-flags/dictation_enabled", json={"value": False})
    assert ok.status_code == 200
    (row,) = audit_rows(world, "feature_flag.changed")
    assert row.detail["from"] is True and row.detail["to"] is False


def test_a_committed_change_always_has_its_audit_row(world, cast):
    """The positive side: every kind of change leaves exactly one row, written with it."""
    created = cast.jadmin.post(
        "/api/admin/assignments",
        role=ADMIN,
        jurisdiction="IAC",
        json={"user_id": str(cast.nobody.user_id), "jurisdiction": "IAC", "role": JUDGE},
    )
    assert created.status_code == 201
    assert len(audit_rows(world, "assignment.added")) == 1
    assert cast.jadmin.delete(f"/api/admin/assignments/{created.json()['id']}", role=ADMIN, jurisdiction="IAC").status_code == 204
    assert len(audit_rows(world, "assignment.removed")) == 1
    assert cast.jadmin.post(f"/api/admin/users/{cast.other.user_id}/deactivate", role=ADMIN, jurisdiction="IAC").status_code == 204
    assert len(audit_rows(world, "user.deactivated")) == 1


# --- M4: approving is one transaction (the request's status and every assignment) ---------------------------------


def test_an_approval_whose_second_assignment_fails_leaves_the_request_pending_and_nothing_assigned(world, cast, monkeypatch):
    request_id = cast.nobody.post("/api/access-requests", json={"jurisdiction": "IAC", "roles": [JUDGE, LTM]}).json()["id"]
    real = assignment_service.ensure_assignment
    calls = []

    def second_fails(session, **kwargs):
        calls.append(kwargs["role"])
        if len(calls) == 2:
            msg = "the second assignment could not be written"
            raise RuntimeError(msg)
        return real(session, **kwargs)

    monkeypatch.setattr(assignment_service, "ensure_assignment", second_fails)
    response = cast.jadmin.post(f"/api/admin/access-requests/{request_id}/approve", role=ADMIN, jurisdiction="IAC", json={})

    assert response.status_code == 500 and len(calls) == 2
    assert world.assignments_of(cast.nobody) == set()  # the first was not kept
    assert cast.nobody.get("/api/access-requests/mine").json()[0]["status"] == "pending"
    assert audit_rows(world, "access_request.approved") == []
    # And the request can still be decided once the fault has gone.
    monkeypatch.setattr(assignment_service, "ensure_assignment", real)
    assert cast.jadmin.post(f"/api/admin/access-requests/{request_id}/approve", role=ADMIN, jurisdiction="IAC", json={}).status_code == 200
    assert world.assignments_of(cast.nobody) == {("IAC", JUDGE), ("IAC", LTM)}
