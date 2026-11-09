"""Access requests (DIAAT-396): request, withdraw, the approvals queue, approve and reject.

The real API with real tokens (tests/unit/admin_fixtures.py): who may ask for what, duplicates,
who sees and decides which requests, that approving writes the assignments in the same transaction
(and that a failed write leaves the request pending), that a request is decided once, and what is
audited. The emails are tests/unit/test_access_request_emails.py.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, select

from tests.unit import auth_fixtures as af
from tests.unit.admin_fixtures import ADMIN, JUDGE, LTM, SUPER, SYSTEM, make_world
from tests.unit.jurisdiction_fixtures import add_jurisdiction
from transcribe_api.domain import assignments as assignment_service
from transcribe_api.domain.models_dictation import AccessRequest, RoleAssignment

pytestmark = pytest.mark.real_jurisdictions


@pytest.fixture(autouse=True)
def no_email(monkeypatch):
    """Nothing here may reach GOV.UK Notify (the test environment sets a placeholder key)."""
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
            "jadmin_iac": world.person("Admin IAC", ("IAC", ADMIN)),
            "jadmin_et": world.person("Admin ET", ("ET", ADMIN)),
            "jsuper": world.person("Super", (None, SUPER)),
            "sysadmin": world.person("Sysadmin", sysadmin=True),
            "judge": world.person("Ivy Judge", ("IAC", JUDGE)),
            "nobody": world.person("Nora Nobody"),
        },
    )


def code(response) -> str:
    return response.json()["detail"]["code"]


def ask(person, where: str, roles: list[str], **kwargs):
    """Ask for ``roles`` in the jurisdiction ``where`` (``role=`` and ``jurisdiction=`` are the context acted in)."""
    return person.post("/api/access-requests", json={"jurisdiction": where, "roles": roles}, **kwargs)


def queue(person, **kwargs):
    return person.get("/api/admin/access-requests", **kwargs)


# --- asking ----------------------------------------------------------------------------------------------


def test_a_user_with_no_role_asks_for_a_role_and_gets_the_contracts_access_request(world, cast):
    response = ask(cast.nobody, "IAC", [LTM, JUDGE])

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"id", "jurisdiction", "roles", "status", "created_at", "decided_at", "reason"}
    assert body["jurisdiction"] == {"code": "IAC", "name": "Immigration and Asylum Chamber"}
    assert body["roles"] == [JUDGE, LTM] and body["status"] == "pending"
    assert body["decided_at"] is None and body["reason"] is None

    # It is what /me/permissions says is pending, and what /mine lists.
    me = cast.nobody.get("/api/me/permissions").json()
    assert [r["id"] for r in me["pending_requests"]] == [body["id"]]
    assert [r["id"] for r in cast.nobody.get("/api/access-requests/mine").json()] == [body["id"]]
    (row,) = world.audit("access_request.created")
    assert row.user_id == str(cast.nobody.user_id) and row.object_id == body["id"]
    assert row.detail == {"jurisdiction": "IAC", "roles": [JUDGE, LTM]}


def test_a_user_who_already_has_a_role_elsewhere_may_ask_for_another_jurisdiction(world, cast):
    response = ask(cast.judge, "ET", [JUDGE], role=JUDGE, jurisdiction="IAC")
    assert response.status_code == 201


@pytest.mark.parametrize(
    "roles",
    [[ADMIN], [SUPER], [SYSTEM], ["Normal"], [JUDGE, ADMIN], ["judge"], [""]],
)
def test_only_judge_and_legal_text_manager_can_be_requested(world, cast, roles):
    response = ask(cast.nobody, "IAC", roles)
    assert response.status_code == 400 and code(response) == "role_not_requestable"
    assert cast.nobody.get("/api/access-requests/mine").json() == []


def test_a_request_needs_a_role(world, cast):
    response = ask(cast.nobody, "IAC", [])
    assert response.status_code == 400 and code(response) == "roles_required"


def test_an_unknown_or_disabled_jurisdiction_is_a_404(world, cast):
    add_jurisdiction(world.engine, "OFF", enabled=False)
    for jurisdiction in ("NOPE", "OFF"):
        response = ask(cast.nobody, jurisdiction, [JUDGE])
        assert response.status_code == 404 and code(response) == "jurisdiction_not_found"


def test_an_identical_pending_request_is_a_409_but_a_different_one_is_not(world, cast):
    assert ask(cast.nobody, "IAC", [JUDGE]).status_code == 201
    duplicate = ask(cast.nobody, "IAC", [JUDGE])
    assert duplicate.status_code == 409 and code(duplicate) == "duplicate_request"
    assert ask(cast.nobody, "iac", [JUDGE]).status_code == 409  # the code's case is no difference
    assert ask(cast.nobody, "IAC", [JUDGE, LTM]).status_code == 201  # not identical
    assert ask(cast.nobody, "ET", [JUDGE]).status_code == 201  # another jurisdiction
    assert len(cast.nobody.get("/api/access-requests/mine").json()) == 3


def test_the_order_of_the_roles_does_not_make_a_request_different(world, cast):
    assert ask(cast.nobody, "IAC", [JUDGE, LTM]).status_code == 201
    assert ask(cast.nobody, "IAC", [LTM, JUDGE, JUDGE]).status_code == 409


def test_a_user_who_already_holds_a_requested_role_there_is_a_409(world, cast):
    held = ask(cast.judge, "IAC", [JUDGE], role=JUDGE, jurisdiction="IAC")
    assert held.status_code == 409 and code(held) == "already_assigned"
    one_of_two = ask(cast.judge, "IAC", [JUDGE, LTM], role=JUDGE, jurisdiction="IAC")
    assert one_of_two.status_code == 409 and code(one_of_two) == "already_assigned"
    # The role they do not hold is theirs to ask for.
    assert ask(cast.judge, "IAC", [LTM], role=JUDGE, jurisdiction="IAC").status_code == 201


def test_a_request_can_be_made_again_after_it_was_withdrawn_rejected_or_approved_and_removed(world, cast):
    first = ask(cast.nobody, "IAC", [JUDGE]).json()
    assert cast.nobody.post(f"/api/access-requests/{first['id']}/withdraw").status_code == 200
    again = ask(cast.nobody, "IAC", [JUDGE])
    assert again.status_code == 201
    assert cast.jadmin_iac.post(f"/api/admin/access-requests/{again.json()['id']}/reject", role=ADMIN, jurisdiction="IAC").status_code == 200
    assert ask(cast.nobody, "IAC", [JUDGE]).status_code == 201


def test_asking_does_not_change_what_anyone_holds(world, cast):
    ask(cast.nobody, "IAC", [JUDGE, LTM])
    assert world.assignments_of(cast.nobody) == set()


def test_the_requests_of_each_user_are_their_own(world, cast):
    ask(cast.nobody, "IAC", [JUDGE])
    assert cast.judge.get("/api/access-requests/mine", role=JUDGE, jurisdiction="IAC").json() == []


def test_a_deactivated_user_cannot_ask(world, cast):
    token = cast.nobody.headers()
    assert cast.jsuper.post(f"/api/admin/users/{cast.nobody.user_id}/deactivate", role=SUPER).status_code == 204
    refused = world.client.post("/api/access-requests", json={"jurisdiction": "IAC", "roles": [JUDGE]}, headers=token)
    assert refused.status_code == 403 and code(refused) == "user_deactivated"


def test_asking_needs_a_token(world, cast):
    response = world.client.post("/api/access-requests", json={"jurisdiction": "IAC", "roles": [JUDGE]})
    assert response.status_code == 401


def test_mine_lists_newest_first_with_every_status(world, cast):
    first = ask(cast.nobody, "IAC", [JUDGE]).json()
    second = ask(cast.nobody, "ET", [LTM]).json()
    cast.nobody.post(f"/api/access-requests/{first['id']}/withdraw")
    mine = cast.nobody.get("/api/access-requests/mine").json()
    assert [(r["id"], r["status"]) for r in mine] == [(second["id"], "pending"), (first["id"], "withdrawn")]


# --- withdrawing ---------------------------------------------------------------------------------------------


def test_a_user_withdraws_their_pending_request_and_it_leaves_the_queue(world, cast):
    created = ask(cast.nobody, "IAC", [JUDGE]).json()
    assert [r["id"] for r in queue(cast.jadmin_iac, role=ADMIN, jurisdiction="IAC").json()] == [created["id"]]

    withdrawn = cast.nobody.post(f"/api/access-requests/{created['id']}/withdraw")

    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "withdrawn" and withdrawn.json()["id"] == created["id"]
    assert queue(cast.jadmin_iac, role=ADMIN, jurisdiction="IAC").json() == []
    assert cast.nobody.get("/api/me/permissions").json()["pending_requests"] == []
    (row,) = world.audit("access_request.withdrawn")
    assert row.user_id == str(cast.nobody.user_id) and row.object_id == created["id"]


def test_only_the_requester_can_withdraw_and_only_while_pending(world, cast):
    created = ask(cast.nobody, "IAC", [JUDGE]).json()
    other = cast.jsuper.post(f"/api/access-requests/{created['id']}/withdraw", role=SUPER)
    assert other.status_code == 404 and code(other) == "request_not_found"
    assert cast.judge.post(f"/api/access-requests/{created['id']}/withdraw", role=JUDGE, jurisdiction="IAC").status_code == 404
    assert cast.nobody.post(f"/api/access-requests/{uuid4()}/withdraw").status_code == 404

    assert cast.jadmin_iac.post(f"/api/admin/access-requests/{created['id']}/approve", role=ADMIN, jurisdiction="IAC").status_code == 200
    late = cast.nobody.post(f"/api/access-requests/{created['id']}/withdraw")
    assert late.status_code == 409 and code(late) == "request_not_pending"
    assert world.assignments_of(cast.nobody) == {("IAC", JUDGE)}  # withdrawing did not take it back


def test_a_withdrawn_request_cannot_be_withdrawn_again_or_decided(world, cast):
    created = ask(cast.nobody, "IAC", [JUDGE]).json()
    cast.nobody.post(f"/api/access-requests/{created['id']}/withdraw")
    assert cast.nobody.post(f"/api/access-requests/{created['id']}/withdraw").status_code == 409
    for decision in ("approve", "reject"):
        response = cast.jadmin_iac.post(f"/api/admin/access-requests/{created['id']}/{decision}", role=ADMIN, jurisdiction="IAC")
        assert response.status_code == 409 and code(response) == "request_not_pending"
    assert world.assignments_of(cast.nobody) == set()


# --- the queue ---------------------------------------------------------------------------------------------


def test_a_jurisdiction_administrator_sees_only_its_jurisdictions_requests_and_a_super_administrator_all(world, cast):
    iac = ask(cast.nobody, "IAC", [JUDGE]).json()
    et = ask(cast.nobody, "ET", [LTM]).json()

    mine = queue(cast.jadmin_iac, role=ADMIN, jurisdiction="IAC")
    assert mine.status_code == 200 and [r["id"] for r in mine.json()] == [iac["id"]]
    (item,) = mine.json()
    assert set(item) == {"id", "jurisdiction", "roles", "status", "created_at", "decided_at", "reason", "requester"}
    assert item["requester"] == {"id": str(cast.nobody.user_id), "email": cast.nobody.email, "name": "Nora Nobody"}
    assert [r["id"] for r in queue(cast.jadmin_et, role=ADMIN, jurisdiction="ET").json()] == [et["id"]]
    everything = queue(cast.jsuper, role=SUPER).json()
    assert sorted(r["id"] for r in everything) == sorted([iac["id"], et["id"]])
    # Oldest first: the queue.
    assert [r["id"] for r in everything] == [iac["id"], et["id"]]


def test_the_queue_shows_pending_by_default_and_other_statuses_on_request(world, cast):
    a = ask(cast.nobody, "IAC", [JUDGE]).json()
    b = ask(cast.nobody, "IAC", [LTM]).json()
    cast.jadmin_iac.post(f"/api/admin/access-requests/{a['id']}/reject", role=ADMIN, jurisdiction="IAC")

    assert [r["id"] for r in queue(cast.jadmin_iac, role=ADMIN, jurisdiction="IAC").json()] == [b["id"]]
    rejected = queue(cast.jadmin_iac, role=ADMIN, jurisdiction="IAC", params={"status": "rejected"}).json()
    assert [r["id"] for r in rejected] == [a["id"]]
    assert len(queue(cast.jadmin_iac, role=ADMIN, jurisdiction="IAC", params={"status": "all"}).json()) == 2
    assert queue(cast.jadmin_iac, role=ADMIN, jurisdiction="IAC", params={"status": "bogus"}).status_code == 422


def test_a_system_administrator_judge_or_user_with_no_role_cannot_use_the_queue(world, cast):
    created = ask(cast.nobody, "IAC", [JUDGE]).json()
    # A SystemAdministrator who has no context to act in is refused for lack of one; in any context, for lack of the permission.
    assert queue(cast.sysadmin).status_code == 403
    for response in (
        cast.sysadmin.post(f"/api/admin/access-requests/{created['id']}/approve"),
        cast.sysadmin.post(f"/api/admin/access-requests/{created['id']}/reject"),
    ):
        assert response.status_code == 403
    sysadmin_judge = world.person("Sysadmin Judge", ("IAC", JUDGE), sysadmin=True)
    for response in (
        queue(sysadmin_judge, role=JUDGE, jurisdiction="IAC"),
        sysadmin_judge.post(f"/api/admin/access-requests/{created['id']}/approve", role=JUDGE, jurisdiction="IAC"),
    ):
        assert response.status_code == 403 and code(response) == "permission_denied"
    assert queue(cast.judge, role=JUDGE, jurisdiction="IAC").status_code == 403
    assert queue(cast.nobody).status_code == 403
    assert world.assignments_of(cast.nobody) == set()
    # A sysadmin who is also a JurisdictionAdministrator may decide, as that role.
    both = world.person("Sysadmin Admin", ("IAC", ADMIN), sysadmin=True)
    assert both.post(f"/api/admin/access-requests/{created['id']}/approve", role=ADMIN, jurisdiction="IAC").status_code == 200


# --- approving and rejecting ----------------------------------------------------------------------------------


def test_approving_gives_exactly_the_requested_roles_in_that_jurisdiction_at_their_next_sign_in(world, cast):
    created = ask(cast.nobody, "IAC", [JUDGE, LTM]).json()
    me = cast.nobody.get("/api/me/permissions").json()
    assert me["status"] == "no_access"

    approved = cast.jadmin_iac.post(
        f"/api/admin/access-requests/{created['id']}/approve", role=ADMIN, jurisdiction="IAC", json={"reason": "Welcome"}
    )

    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "approved" and body["decided_at"] and body["reason"] == "Welcome"
    assert set(body) == {"id", "jurisdiction", "roles", "status", "created_at", "decided_at", "reason"}
    assert world.assignments_of(cast.nobody) == {("IAC", JUDGE), ("IAC", LTM)}
    with Session(world.engine) as session:
        assert {r.granted_by for r in session.exec(select(RoleAssignment).where(RoleAssignment.user_id == cast.nobody.user_id))} == {
            cast.jadmin_iac.user_id
        }
        stored = session.get(AccessRequest, UUID(created["id"]))
        assert stored.decided_by == cast.jadmin_iac.user_id

    # The requester's next sign-in shows the new assignments (two pairs: they choose), and nothing pending.
    again = cast.nobody.sign_in().json()["me"]
    assert again["status"] == "choose"
    assert sorted(a["role"] for a in again["assignments"]) == [JUDGE, LTM]
    assert again["pending_requests"] == []
    assert cast.nobody.get("/api/access-requests/mine").json()[0]["status"] == "approved"
    # And it left the queue.
    assert queue(cast.jadmin_iac, role=ADMIN, jurisdiction="IAC").json() == []


def test_a_super_administrator_decides_requests_for_any_jurisdiction(world, cast):
    for jurisdiction in ("IAC", "ET"):
        created = ask(cast.nobody, jurisdiction, [JUDGE]).json()
        assert cast.jsuper.post(f"/api/admin/access-requests/{created['id']}/approve", role=SUPER).status_code == 200
    assert world.assignments_of(cast.nobody) == {("IAC", JUDGE), ("ET", JUDGE)}


def test_a_jurisdiction_administrator_cannot_decide_another_jurisdictions_request(world, cast):
    created = ask(cast.nobody, "IAC", [JUDGE]).json()
    for decision in ("approve", "reject"):
        response = cast.jadmin_et.post(f"/api/admin/access-requests/{created['id']}/{decision}", role=ADMIN, jurisdiction="ET")
        assert response.status_code == 403 and code(response) == "jurisdiction_not_in_scope"
    assert world.assignments_of(cast.nobody) == set()
    assert cast.nobody.get("/api/access-requests/mine").json()[0]["status"] == "pending"


def test_rejecting_gives_nothing_and_the_reason_is_visible_to_the_requester(world, cast):
    created = ask(cast.nobody, "IAC", [JUDGE]).json()
    rejected = cast.jadmin_iac.post(
        f"/api/admin/access-requests/{created['id']}/reject", role=ADMIN, jurisdiction="IAC", json={"reason": "Not a member"}
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected" and rejected.json()["reason"] == "Not a member"
    assert world.assignments_of(cast.nobody) == set()
    mine = cast.nobody.get("/api/access-requests/mine").json()
    assert mine[0]["status"] == "rejected" and mine[0]["reason"] == "Not a member" and mine[0]["decided_at"]


def test_the_reason_is_optional_and_the_body_may_be_left_out(world, cast):
    one = ask(cast.nobody, "IAC", [JUDGE]).json()
    two = ask(cast.nobody, "ET", [JUDGE]).json()
    assert cast.jsuper.post(f"/api/admin/access-requests/{one['id']}/approve", role=SUPER).json()["reason"] is None
    blank = cast.jsuper.post(f"/api/admin/access-requests/{two['id']}/reject", role=SUPER, json={"reason": "   "})
    assert blank.status_code == 200 and blank.json()["reason"] is None
    long = ask(cast.nobody, "IAC", [LTM]).json()
    assert cast.jsuper.post(f"/api/admin/access-requests/{long['id']}/reject", role=SUPER, json={"reason": "x" * 501}).status_code == 422


@pytest.mark.parametrize("first", ["approve", "reject"])
@pytest.mark.parametrize("second", ["approve", "reject"])
def test_a_decided_request_cannot_be_decided_again(world, cast, first, second):
    created = ask(cast.nobody, "IAC", [JUDGE]).json()
    assert cast.jadmin_iac.post(f"/api/admin/access-requests/{created['id']}/{first}", role=ADMIN, jurisdiction="IAC").status_code == 200
    again = cast.jsuper.post(f"/api/admin/access-requests/{created['id']}/{second}", role=SUPER)
    assert again.status_code == 409 and code(again) == "request_not_pending"
    expected = {("IAC", JUDGE)} if first == "approve" else set()
    assert world.assignments_of(cast.nobody) == expected


def test_an_unknown_request_is_a_404(world, cast):
    for decision in ("approve", "reject"):
        response = cast.jsuper.post(f"/api/admin/access-requests/{uuid4()}/{decision}", role=SUPER)
        assert response.status_code == 404 and code(response) == "request_not_found"


def test_approving_is_idempotent_for_a_role_the_user_already_holds(world, cast):
    created = ask(cast.nobody, "IAC", [JUDGE, LTM]).json()
    # Someone assigned them Judge meanwhile.
    assert cast.jsuper.post(
        "/api/admin/assignments", role=SUPER, json={"user_id": str(cast.nobody.user_id), "jurisdiction": "IAC", "role": JUDGE}
    ).status_code == 201

    approved = cast.jadmin_iac.post(f"/api/admin/access-requests/{created['id']}/approve", role=ADMIN, jurisdiction="IAC")

    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    with Session(world.engine) as session:
        rows = session.exec(select(RoleAssignment).where(RoleAssignment.user_id == cast.nobody.user_id)).all()
    assert sorted(r.role for r in rows) == [JUDGE, LTM]  # no duplicate Judge


def test_if_the_assignments_cannot_be_written_the_request_stays_pending_and_nothing_is_written(world, cast, monkeypatch):
    """Approving is one transaction: a failure after the first role leaves no assignment and no decision."""
    created = ask(cast.nobody, "IAC", [JUDGE, LTM]).json()
    real = assignment_service.ensure_assignment
    calls = []

    def second_fails(session, **kwargs):
        calls.append(kwargs["role"])
        if len(calls) == 2:
            msg = "disk full"
            raise RuntimeError(msg)
        return real(session, **kwargs)

    monkeypatch.setattr(assignment_service, "ensure_assignment", second_fails)
    response = cast.jadmin_iac.post(f"/api/admin/access-requests/{created['id']}/approve", role=ADMIN, jurisdiction="IAC")

    assert response.status_code == 500 and len(calls) == 2
    assert world.assignments_of(cast.nobody) == set()
    stored = cast.nobody.get("/api/access-requests/mine").json()[0]
    assert stored["status"] == "pending" and stored["decided_at"] is None
    assert [r["id"] for r in queue(cast.jadmin_iac, role=ADMIN, jurisdiction="IAC").json()] == [created["id"]]
    assert world.audit("access_request.approved") == []

    # And it can still be approved once the fault is gone.
    monkeypatch.setattr(assignment_service, "ensure_assignment", real)
    assert cast.jadmin_iac.post(f"/api/admin/access-requests/{created['id']}/approve", role=ADMIN, jurisdiction="IAC").status_code == 200
    assert world.assignments_of(cast.nobody) == {("IAC", JUDGE), ("IAC", LTM)}


def test_only_the_requested_roles_are_ever_written_even_for_an_administrator_approver(world, cast):
    created = ask(cast.nobody, "IAC", [JUDGE]).json()
    cast.jsuper.post(f"/api/admin/access-requests/{created['id']}/approve", role=SUPER)
    assert world.assignments_of(cast.nobody) == {("IAC", JUDGE)}


# --- the audit and the history ----------------------------------------------------------------------------------


def test_requests_and_decisions_are_audited_with_the_approvers_active_context(world, cast):
    approved = ask(cast.nobody, "IAC", [JUDGE, LTM]).json()
    rejected = ask(cast.nobody, "ET", [JUDGE]).json()
    cast.jadmin_iac.post(f"/api/admin/access-requests/{approved['id']}/approve", role=ADMIN, jurisdiction="IAC")
    cast.jadmin_et.post(f"/api/admin/access-requests/{rejected['id']}/reject", role=ADMIN, jurisdiction="ET", json={"reason": "no"})

    rows = world.audit("access_request.approved")
    assert sorted(r.detail["role"] for r in rows) == [JUDGE, LTM]  # one row per role
    for row in rows:
        assert row.user_id == str(cast.jadmin_iac.user_id)
        assert row.active_role == ADMIN and row.active_jurisdiction_id == world.iac.id
        assert row.detail["target_user_id"] == str(cast.nobody.user_id) and row.detail["request_id"] == approved["id"]
        assert row.detail["jurisdiction"] == "IAC"
    (row,) = world.audit("access_request.rejected")
    assert row.user_id == str(cast.jadmin_et.user_id) and row.active_jurisdiction_id == world.et.id
    assert row.detail["role"] == JUDGE and row.detail["request_id"] == rejected["id"]
    # The reason is the requester's to read, not the audit log's to carry.
    assert "no" not in {str(v) for v in row.detail.values()}


def test_decisions_are_in_the_role_history_of_their_jurisdiction(world, cast):
    one = ask(cast.nobody, "IAC", [JUDGE]).json()
    two = ask(cast.nobody, "ET", [LTM]).json()
    cast.jadmin_iac.post(f"/api/admin/access-requests/{one['id']}/approve", role=ADMIN, jurisdiction="IAC")
    cast.jadmin_et.post(f"/api/admin/access-requests/{two['id']}/reject", role=ADMIN, jurisdiction="ET")

    iac = cast.jadmin_iac.get("/api/admin/role-history", role=ADMIN, jurisdiction="IAC").json()["items"]
    assert [(e["change"], e["jurisdiction"], e["role"]) for e in iac] == [("access_request.approved", "IAC", JUDGE)]
    assert iac[0]["target"] == {"id": str(cast.nobody.user_id), "email": cast.nobody.email}
    assert iac[0]["actor_context"] == {"jurisdiction": "IAC", "role": ADMIN}
    everything = cast.jsuper.get("/api/admin/role-history", role=SUPER).json()["items"]
    assert sorted(e["change"] for e in everything) == ["access_request.approved", "access_request.rejected"]


def test_every_change_the_history_documents_can_appear(world, cast):
    """The contract's seven changes, each produced by its real endpoint."""
    assert cast.jsuper.post(
        "/api/admin/assignments", role=SUPER, json={"user_id": str(cast.nobody.user_id), "jurisdiction": "IAC", "role": JUDGE}
    ).status_code == 201
    assignment = cast.jsuper.get("/api/admin/users", role=SUPER, params={"search": cast.nobody.email}).json()["items"][0]["assignments"][0]
    assert cast.jsuper.delete(f"/api/admin/assignments/{assignment['id']}", role=SUPER).status_code == 204
    cast.jsuper.post(f"/api/admin/users/{cast.nobody.user_id}/deactivate", role=SUPER)
    cast.jsuper.post(f"/api/admin/users/{cast.nobody.user_id}/reactivate", role=SUPER)
    cast.judge.delete("/api/me/jurisdictions/IAC", role=JUDGE, jurisdiction="IAC")
    requester = world.person("Ria Requester")
    for jurisdiction, decision in (("IAC", "approve"), ("ET", "reject")):
        created = ask(requester, jurisdiction, [JUDGE]).json()
        cast.jsuper.post(f"/api/admin/access-requests/{created['id']}/{decision}", role=SUPER)

    seen = {e["change"] for e in cast.jsuper.get("/api/admin/role-history", role=SUPER, params={"page_size": 100}).json()["items"]}
    assert seen == {
        "assignment.added",
        "assignment.removed",
        "user.deactivated",
        "user.reactivated",
        "membership.leave",
        "access_request.approved",
        "access_request.rejected",
    }
