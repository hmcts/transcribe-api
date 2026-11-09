"""Who may assign what, and the assignment endpoints (DIAAT-383, DIAAT-391, DIAAT-394).

Every case of the contract's section 1, as the real API answers it: allowed and each way of being
refused, with the 403 codes (``assignment_not_allowed``, ``jurisdiction_not_in_scope``), what is
written to the database and to the audit log, and the pending (email-only) assignments that are claimed
at a first sign-in and can be removed by the id the user list gave them.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, select

from tests.unit import auth_fixtures as af
from tests.unit.admin_fixtures import ADMIN, JUDGE, LTM, SUPER, SYSTEM, Person, make_world
from transcribe_api.domain.models_dictation import RoleAssignment

pytestmark = pytest.mark.real_jurisdictions


@pytest.fixture
def world(jurisdiction_db, monkeypatch):
    yield make_world(jurisdiction_db, monkeypatch)
    af.unstub_entra()


@pytest.fixture
def cast(world):
    """The people of the contract's table, plus a target with no role yet."""
    return type(
        "Cast",
        (),
        {
            "jadmin_iac": world.person("Admin IAC", ("IAC", ADMIN)),
            "jadmin_et": world.person("Admin ET", ("ET", ADMIN)),
            "jsuper": world.person("Super", (None, SUPER)),
            "sysadmin": world.person("Sysadmin", sysadmin=True),
            "judge": world.person("Judge IAC", ("IAC", JUDGE)),
            "bob": world.person("Bob Target"),
        },
    )


def assign(actor: Person, target: Person | None, jurisdiction: str | None, role: str, *, as_role=None, at=None, **extra):
    body = {"user_id": str(target.user_id) if target else None, "email": None, "jurisdiction": jurisdiction, "role": role}
    body.update(extra)
    return actor.post("/api/admin/assignments", role=as_role, jurisdiction=at, json=body)


def code(response) -> str:
    return response.json()["detail"]["code"]


# --- JurisdictionAdministrator: Judge and LegalTextManager in its own jurisdiction only ----------------


@pytest.mark.parametrize("role", [JUDGE, LTM])
def test_a_jurisdiction_administrator_assigns_judge_and_legal_text_manager_in_its_jurisdiction(world, cast, role):
    response = assign(cast.jadmin_iac, cast.bob, "IAC", role, as_role=ADMIN, at="IAC")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] and body["role"] == role and body["user_id"] == str(cast.bob.user_id)
    assert body["jurisdiction"] == {"code": "IAC", "name": "Immigration and Asylum Chamber"}
    assert body["pending"] is False and body["email"] == cast.bob.email
    assert world.assignments_of(cast.bob) == {("IAC", role)}
    with Session(world.engine) as session:
        row = session.get(RoleAssignment, UUID(body["id"]))
        assert row.granted_by == cast.jadmin_iac.user_id


@pytest.mark.parametrize("role", [JUDGE, LTM])
def test_a_jurisdiction_administrator_cannot_assign_in_another_jurisdiction(world, cast, role):
    response = assign(cast.jadmin_iac, cast.bob, "ET", role, as_role=ADMIN, at="IAC")

    assert response.status_code == 403 and code(response) == "jurisdiction_not_in_scope"
    assert world.assignments_of(cast.bob) == set()


@pytest.mark.parametrize("jurisdiction", ["IAC", "ET"])
def test_a_jurisdiction_administrator_never_assigns_jurisdiction_administrator(world, cast, jurisdiction):
    response = assign(cast.jadmin_iac, cast.bob, jurisdiction, ADMIN, as_role=ADMIN, at="IAC")

    assert response.status_code == 403 and code(response) == "assignment_not_allowed"
    assert world.assignments_of(cast.bob) == set()


def test_a_jurisdiction_administrator_never_assigns_super_administrator_or_system_administrator(world, cast):
    for jurisdiction, role in ((None, SUPER), ("IAC", SUPER), ("IAC", SYSTEM), (None, SYSTEM)):
        response = assign(cast.jadmin_iac, cast.bob, jurisdiction, role, as_role=ADMIN, at="IAC")
        assert response.status_code == 403 and code(response) == "assignment_not_allowed", (jurisdiction, role)
    assert world.assignments_of(cast.bob) == set()


# --- JurisdictionSuperAdministrator: any jurisdiction, and JurisdictionAdministrator ------------------


@pytest.mark.parametrize("jurisdiction", ["IAC", "ET"])
@pytest.mark.parametrize("role", [JUDGE, LTM, ADMIN])
def test_a_super_administrator_assigns_judge_legal_text_manager_and_administrator_in_any_jurisdiction(
    world, cast, jurisdiction, role
):
    response = assign(cast.jsuper, cast.bob, jurisdiction, role, as_role=SUPER)

    assert response.status_code == 201, response.text
    assert world.assignments_of(cast.bob) == {(jurisdiction, role)}


def test_only_a_system_administrator_assigns_the_super_administrator_role(world, cast):
    refused = assign(cast.jsuper, cast.bob, None, SUPER, as_role=SUPER)
    assert refused.status_code == 403 and code(refused) == "assignment_not_allowed"
    assert world.assignments_of(cast.bob) == set()

    allowed = assign(cast.sysadmin, cast.bob, None, SUPER)
    assert allowed.status_code == 201 and allowed.json()["jurisdiction"] is None
    assert world.assignments_of(cast.bob) == {(None, SUPER)}


def test_nobody_can_grant_system_administrator(world, cast):
    for actor, as_role, at in (
        (cast.jsuper, SUPER, None),
        (cast.sysadmin, None, None),
        (cast.jadmin_iac, ADMIN, "IAC"),
    ):
        for jurisdiction in (None, "IAC"):
            response = assign(actor, cast.bob, jurisdiction, SYSTEM, as_role=as_role, at=at)
            assert response.status_code == 403 and code(response) == "assignment_not_allowed"
    # Not even to themselves.
    response = assign(cast.sysadmin, cast.sysadmin, None, SYSTEM)
    assert response.status_code == 403 and code(response) == "assignment_not_allowed"
    with Session(world.engine) as session:
        assert session.exec(select(RoleAssignment).where(RoleAssignment.role == SYSTEM)).all() == []


@pytest.mark.parametrize("role", ["Normal", "Admin", "judge", ""])
def test_a_role_that_does_not_exist_is_a_400(world, cast, role):
    response = assign(cast.jsuper, cast.bob, "IAC", role, as_role=SUPER)
    assert response.status_code == 400 and code(response) == "invalid_role"


# --- SystemAdministrator: the two administrator roles for others, anything for themselves -------------


def test_a_system_administrator_assigns_jurisdiction_administrator_in_any_jurisdiction(world, cast):
    for jurisdiction in ("IAC", "ET"):
        response = assign(cast.sysadmin, cast.bob, jurisdiction, ADMIN)
        assert response.status_code == 201, response.text
    assert world.assignments_of(cast.bob) == {("IAC", ADMIN), ("ET", ADMIN)}


@pytest.mark.parametrize("role", [JUDGE, LTM])
def test_a_system_administrator_does_not_assign_judge_or_legal_text_manager_to_others(world, cast, role):
    response = assign(cast.sysadmin, cast.bob, "IAC", role)
    assert response.status_code == 403 and code(response) == "assignment_not_allowed"
    assert world.assignments_of(cast.bob) == set()


def test_a_system_administrator_can_give_themselves_super_administrator_and_a_role_in_a_jurisdiction(world, cast):
    """How the first JurisdictionSuperAdministrator of an environment is set up (DIAAT-394)."""
    for jurisdiction, role in ((None, SUPER), ("IAC", ADMIN), ("ET", JUDGE), ("ET", LTM)):
        response = assign(cast.sysadmin, cast.sysadmin, jurisdiction, role)
        assert response.status_code == 201, (jurisdiction, role, response.text)
    assert world.assignments_of(cast.sysadmin) == {(None, SUPER), ("IAC", ADMIN), ("ET", JUDGE), ("ET", LTM)}
    # And they can take it away again.
    listed = cast.sysadmin.get("/api/admin/users", params={"search": cast.sysadmin.email}).json()["items"]
    (mine,) = listed
    for assignment in mine["assignments"]:
        assert cast.sysadmin.delete(f"/api/admin/assignments/{assignment['id']}").status_code == 204
    assert world.assignments_of(cast.sysadmin) == set()


# --- nobody else changes their own administrator role ---------------------------------------------------


def test_a_jurisdiction_administrator_cannot_assign_or_remove_their_own_administrator_role(world, cast):
    own = next(iter(world.assignments_of(cast.jadmin_iac)))
    assert own == ("IAC", ADMIN)
    refused = assign(cast.jadmin_iac, cast.jadmin_iac, "IAC", ADMIN, as_role=ADMIN, at="IAC")
    assert refused.status_code == 403 and code(refused) == "assignment_not_allowed"

    listed = cast.jsuper.get("/api/admin/users", params={"search": cast.jadmin_iac.email}, role=SUPER).json()["items"]
    assignment_id = listed[0]["assignments"][0]["id"]
    removal = cast.jadmin_iac.delete(f"/api/admin/assignments/{assignment_id}", role=ADMIN, jurisdiction="IAC")
    assert removal.status_code == 403 and code(removal) == "assignment_not_allowed"
    assert world.assignments_of(cast.jadmin_iac) == {("IAC", ADMIN)}


def test_a_super_administrator_cannot_change_their_own_administrator_roles(world, cast):
    refused = assign(cast.jsuper, cast.jsuper, "ET", ADMIN, as_role=SUPER)
    assert refused.status_code == 403 and code(refused) == "assignment_not_allowed"
    refused = assign(cast.jsuper, cast.jsuper, None, SUPER, as_role=SUPER)
    assert refused.status_code == 403 and code(refused) == "assignment_not_allowed"
    listed = cast.jsuper.get("/api/admin/users", params={"search": cast.jsuper.email}, role=SUPER).json()["items"]
    removal = cast.jsuper.delete(f"/api/admin/assignments/{listed[0]['assignments'][0]['id']}", role=SUPER)
    assert removal.status_code == 403 and code(removal) == "assignment_not_allowed"
    assert world.assignments_of(cast.jsuper) == {(None, SUPER)}


def test_nobody_gives_themselves_any_role_not_even_judge(world, cast):
    """Review finding S-low (a): a JurisdictionAdministrator or JurisdictionSuperAdministrator gave themselves Judge
    (recording and dictation access) and approved their own requests. Now no one gives themselves a role."""
    for person, role, where, jurisdiction in (
        (cast.jadmin_iac, ADMIN, "IAC", "IAC"),
        (cast.jsuper, SUPER, None, "ET"),
    ):
        for given in (JUDGE, LTM):
            response = assign(person, person, jurisdiction, given, as_role=role, at=where)
            assert response.status_code == 403 and code(response) == "assignment_not_allowed", (role, given)
    assert world.assignments_of(cast.jadmin_iac) == {("IAC", ADMIN)}
    assert world.assignments_of(cast.jsuper) == {(None, SUPER)}


def test_the_email_of_your_own_account_is_still_you(world, cast):
    """Naming yourself by email is not a way round the rule."""
    response = cast.jadmin_iac.post(
        "/api/admin/assignments",
        role=ADMIN,
        jurisdiction="IAC",
        json={"email": cast.jadmin_iac.email.upper(), "jurisdiction": "ET", "role": ADMIN},
    )
    assert response.status_code == 403 and code(response) == "assignment_not_allowed"


# --- who may call at all ----------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["judge", "bob"])
def test_a_judge_or_a_user_with_no_role_may_not_assign(world, cast, who):
    person = getattr(cast, who)
    role = JUDGE if who == "judge" else None
    response = assign(person, cast.bob, "IAC", JUDGE, as_role=role, at="IAC" if role else None)
    assert response.status_code == 403
    assert code(response) == ("permission_denied" if who == "judge" else "context_required")
    assert world.assignments_of(cast.bob) == set()


# --- what is a valid assignment -----------------------------------------------------------------------


def test_the_target_is_a_user_or_an_email_not_both_and_not_neither(world, cast):
    both = cast.jsuper.post(
        "/api/admin/assignments",
        role=SUPER,
        json={"user_id": str(cast.bob.user_id), "email": "x@y.test", "jurisdiction": "IAC", "role": JUDGE},
    )
    neither = cast.jsuper.post("/api/admin/assignments", role=SUPER, json={"jurisdiction": "IAC", "role": JUDGE})
    assert both.status_code == neither.status_code == 400
    assert code(both) == code(neither) == "invalid_target"


def test_a_role_in_a_jurisdiction_needs_the_jurisdiction_and_the_global_role_has_none(world, cast):
    no_jurisdiction = assign(cast.jsuper, cast.bob, None, JUDGE, as_role=SUPER)
    assert no_jurisdiction.status_code == 400 and code(no_jurisdiction) == "invalid_assignment"
    admin_without = assign(cast.jsuper, cast.bob, None, ADMIN, as_role=SUPER)
    assert admin_without.status_code == 400 and code(admin_without) == "invalid_assignment"
    global_in_a_jurisdiction = assign(cast.sysadmin, cast.bob, "IAC", SUPER)
    assert global_in_a_jurisdiction.status_code == 400 and code(global_in_a_jurisdiction) == "invalid_assignment"


def test_an_unknown_user_or_jurisdiction_is_a_404(world, cast):
    response = cast.jsuper.post(
        "/api/admin/assignments",
        role=SUPER,
        json={"user_id": str(uuid4()), "jurisdiction": "IAC", "role": JUDGE},
    )
    assert response.status_code == 404 and code(response) == "user_not_found"
    response = assign(cast.jsuper, cast.bob, "NOPE", JUDGE, as_role=SUPER)
    assert response.status_code == 404 and code(response) == "jurisdiction_not_found"


def test_a_malformed_email_is_a_400(world, cast):
    response = cast.jsuper.post(
        "/api/admin/assignments", role=SUPER, json={"email": "not an email", "jurisdiction": "IAC", "role": JUDGE}
    )
    assert response.status_code == 400 and code(response) == "invalid_email"


def test_assigning_what_they_already_hold_is_a_409(world, cast):
    assert assign(cast.jsuper, cast.bob, "IAC", JUDGE, as_role=SUPER).status_code == 201
    again = assign(cast.jsuper, cast.bob, "IAC", JUDGE, as_role=SUPER)
    assert again.status_code == 409 and code(again) == "already_assigned"
    assert world.assignments_of(cast.bob) == {("IAC", JUDGE)}


def test_the_same_person_holds_several_roles_and_several_jurisdictions(world, cast):
    for jurisdiction, role in (("IAC", JUDGE), ("IAC", LTM), ("ET", JUDGE)):
        assert assign(cast.jsuper, cast.bob, jurisdiction, role, as_role=SUPER).status_code == 201
    assert world.assignments_of(cast.bob) == {("IAC", JUDGE), ("IAC", LTM), ("ET", JUDGE)}


# --- every change is audited -----------------------------------------------------------------------------


def test_an_assignment_is_audited_with_actor_active_context_target_and_change(world, cast):
    response = assign(cast.jadmin_iac, cast.bob, "IAC", JUDGE, as_role=ADMIN, at="IAC")
    assert response.status_code == 201

    (row,) = world.audit("assignment.added")
    assert row.user_id == str(cast.jadmin_iac.user_id) and row.outcome == "success"
    assert row.active_role == ADMIN and row.active_jurisdiction_id == world.iac.id
    assert row.detail["target_user_id"] == str(cast.bob.user_id)
    assert row.detail["jurisdiction"] == "IAC" and row.detail["role"] == JUDGE
    # No contact details of someone who has signed in.
    assert cast.bob.email not in str(row.detail)

    assert cast.jadmin_iac.delete(f"/api/admin/assignments/{response.json()['id']}", role=ADMIN, jurisdiction="IAC").status_code == 204
    (removed,) = world.audit("assignment.removed")
    assert removed.active_role == ADMIN and removed.detail["role"] == JUDGE
    assert removed.detail["target_user_id"] == str(cast.bob.user_id)


def test_a_system_administrator_with_no_context_is_audited_with_no_active_role(world, cast):
    assert assign(cast.sysadmin, cast.bob, "ET", ADMIN).status_code == 201
    (row,) = world.audit("assignment.added")
    assert row.user_id == str(cast.sysadmin.user_id) and row.active_role is None


def test_a_refused_change_is_audited_as_denied_and_writes_no_assignment(world, cast):
    assign(cast.jadmin_iac, cast.bob, "IAC", ADMIN, as_role=ADMIN, at="IAC")
    assign(cast.jadmin_iac, cast.bob, "ET", JUDGE, as_role=ADMIN, at="IAC")

    denied = world.audit("ACCESS_DENIED")
    assert sorted(row.detail["reason"] for row in denied) == ["assignment_not_allowed", "jurisdiction_not_in_scope"]
    assert all(row.outcome == "denied" and row.active_role == ADMIN for row in denied)
    assert world.audit("assignment.added") == []


# --- removing an assignment -----------------------------------------------------------------------------


def _assignment_id(world, person: Person, jurisdiction: str | None, role: str) -> str:
    with Session(world.engine) as session:
        for row in session.exec(select(RoleAssignment).where(RoleAssignment.user_id == person.user_id)).all():
            held_in = None
            if row.jurisdiction_id:
                held_in = next(code for code, j in world.jurisdictions.items() if j.id == row.jurisdiction_id)
            if (held_in, row.role) == (jurisdiction, role):
                return str(row.id)
    raise AssertionError("not assigned")


def test_removing_follows_the_same_rules_as_assigning(world, cast):
    world.give(cast.bob, "IAC", JUDGE)
    world.give(cast.bob, "ET", JUDGE)
    world.give(cast.bob, "IAC", ADMIN)
    iac_judge = _assignment_id(world, cast.bob, "IAC", JUDGE)
    et_judge = _assignment_id(world, cast.bob, "ET", JUDGE)
    iac_admin = _assignment_id(world, cast.bob, "IAC", ADMIN)

    # A JurisdictionAdministrator: Judge in its own jurisdiction only; never an administrator.
    other = cast.jadmin_iac.delete(f"/api/admin/assignments/{et_judge}", role=ADMIN, jurisdiction="IAC")
    assert other.status_code == 403 and code(other) == "jurisdiction_not_in_scope"
    admin = cast.jadmin_iac.delete(f"/api/admin/assignments/{iac_admin}", role=ADMIN, jurisdiction="IAC")
    assert admin.status_code == 403 and code(admin) == "assignment_not_allowed"
    assert world.assignments_of(cast.bob) == {("IAC", JUDGE), ("ET", JUDGE), ("IAC", ADMIN)}
    assert cast.jadmin_iac.delete(f"/api/admin/assignments/{iac_judge}", role=ADMIN, jurisdiction="IAC").status_code == 204

    # A system administrator removes administrators, not judges.
    judge = cast.sysadmin.delete(f"/api/admin/assignments/{et_judge}")
    assert judge.status_code == 403 and code(judge) == "assignment_not_allowed"
    assert cast.sysadmin.delete(f"/api/admin/assignments/{iac_admin}").status_code == 204

    # A super administrator removes judges anywhere.
    assert cast.jsuper.delete(f"/api/admin/assignments/{et_judge}", role=SUPER).status_code == 204
    assert world.assignments_of(cast.bob) == set()


def test_only_a_system_administrator_removes_a_super_administrator(world, cast):
    world.give(cast.bob, None, SUPER)
    assignment = _assignment_id(world, cast.bob, None, SUPER)
    refused = cast.jsuper.delete(f"/api/admin/assignments/{assignment}", role=SUPER)
    assert refused.status_code == 403 and code(refused) == "assignment_not_allowed"
    assert cast.sysadmin.delete(f"/api/admin/assignments/{assignment}").status_code == 204
    assert world.assignments_of(cast.bob) == set()


def test_removing_an_unknown_assignment_is_a_404(world, cast):
    response = cast.jsuper.delete(f"/api/admin/assignments/{uuid4()}", role=SUPER)
    assert response.status_code == 404 and code(response) == "assignment_not_found"


# --- GET /api/admin/roles says what the rules allow ------------------------------------------------------


@pytest.mark.parametrize(
    ("who", "as_role", "at", "offered"),
    [
        ("jadmin_iac", ADMIN, "IAC", [JUDGE, LTM]),
        ("jsuper", SUPER, None, [JUDGE, LTM, ADMIN]),
        ("sysadmin", None, None, [ADMIN, SUPER]),
    ],
)
def test_the_roles_endpoint_offers_exactly_the_roles_the_rules_let_the_caller_assign(
    world, cast, who, as_role, at, offered
):
    actor = getattr(cast, who)
    listed = actor.get("/api/admin/roles", role=as_role, jurisdiction=at)
    assert listed.status_code == 200 and listed.json() == {"roles": offered}
    assert SYSTEM not in listed.json()["roles"]

    # What it lists, the caller may assign to someone else (a fresh target each time) ...
    for role in offered:
        target = world.person(f"Target {role}")
        response = assign(actor, target, None if role == SUPER else "IAC", role, as_role=as_role, at=at)
        assert response.status_code == 201, (who, role, response.text)
    # ... and every other stored role they may not.
    for role in (JUDGE, LTM, ADMIN, SUPER):
        if role in offered:
            continue
        target = world.person(f"Not {role}")
        response = assign(actor, target, None if role == SUPER else "IAC", role, as_role=as_role, at=at)
        assert response.status_code == 403, (who, role)
        assert code(response) == "assignment_not_allowed"


def test_the_roles_endpoint_shape_is_pinned(world, cast):
    body = cast.jsuper.get("/api/admin/roles", role=SUPER).json()
    assert set(body) == {"roles"} and isinstance(body["roles"], list)
    assert all(isinstance(role, str) for role in body["roles"])
    assert cast.judge.get("/api/admin/roles", role=JUDGE, jurisdiction="IAC").status_code == 403


# --- pending assignments, by email ------------------------------------------------------------------------


def test_an_assignment_by_email_is_pending_and_claimed_at_the_first_sign_in(world, cast):
    email = "New.Judge@Justice.gov.uk"
    response = cast.jadmin_iac.post(
        "/api/admin/assignments",
        role=ADMIN,
        jurisdiction="IAC",
        json={"email": f"  {email}  ", "jurisdiction": "IAC", "role": JUDGE},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["pending"] is True and body["user_id"] is None and body["email"] == email.lower()
    assert body["id"] and body["jurisdiction"]["code"] == "IAC"
    assert world.pending_for(email) == {("IAC", JUDGE)}
    (row,) = world.audit("assignment.added")
    # Nobody to name by id: the address is kept, to say who it was for.
    assert row.detail["target_user_id"] is None and row.detail["target_email"] == email.lower()

    newcomer = Person(world, "New Judge", email=email)
    signed_in = newcomer.sign_in()
    me = signed_in.json()["me"]
    assert me["status"] == "ready"
    assert me["assignments"] == [{"jurisdiction": {"code": "IAC", "name": "Immigration and Asylum Chamber"}, "role": JUDGE}]
    assert world.pending_for(email) == set()
    assert world.assignments_of(newcomer) == {("IAC", JUDGE)}


def test_the_rules_apply_to_a_pending_assignment_too(world, cast):
    for jurisdiction, role, want in (("ET", JUDGE, "jurisdiction_not_in_scope"), ("IAC", ADMIN, "assignment_not_allowed")):
        response = cast.jadmin_iac.post(
            "/api/admin/assignments",
            role=ADMIN,
            jurisdiction="IAC",
            json={"email": "later@justice.gov.uk", "jurisdiction": jurisdiction, "role": role},
        )
        assert response.status_code == 403 and code(response) == want
    assert world.pending_for("later@justice.gov.uk") == set()


def test_an_email_that_belongs_to_someone_who_has_signed_in_is_assigned_to_them(world, cast):
    response = cast.jadmin_iac.post(
        "/api/admin/assignments",
        role=ADMIN,
        jurisdiction="IAC",
        json={"email": cast.bob.email.upper(), "jurisdiction": "IAC", "role": JUDGE},
    )
    assert response.status_code == 201
    assert response.json()["pending"] is False and response.json()["user_id"] == str(cast.bob.user_id)
    assert world.assignments_of(cast.bob) == {("IAC", JUDGE)}
    assert world.pending_for(cast.bob.email) == set()


def test_a_pending_assignments_id_works_with_delete_and_nothing_is_claimed_afterwards(world, cast):
    email = "never@justice.gov.uk"
    created = cast.jadmin_iac.post(
        "/api/admin/assignments",
        role=ADMIN,
        jurisdiction="IAC",
        json={"email": email, "jurisdiction": "IAC", "role": LTM},
    ).json()

    # The user list shows it, with the id DELETE takes.
    listed = cast.jadmin_iac.get("/api/admin/users", role=ADMIN, jurisdiction="IAC", params={"search": "never"}).json()
    (row,) = [item for item in listed["items"] if item["email"] == email]
    assert row["pending"] is True and [a["id"] for a in row["assignments"]] == [created["id"]]

    assert cast.jadmin_et.delete(f"/api/admin/assignments/{created['id']}", role=ADMIN, jurisdiction="ET").status_code == 403
    assert cast.jadmin_iac.delete(f"/api/admin/assignments/{created['id']}", role=ADMIN, jurisdiction="IAC").status_code == 204
    assert world.pending_for(email) == set()
    (removed,) = world.audit("assignment.removed")
    assert removed.detail["target_email"] == email and removed.detail["target_user_id"] is None

    # They sign in later and get nothing.
    me = Person(world, "Never", email=email).sign_in().json()["me"]
    assert me["status"] == "no_access" and me["assignments"] == []
    assert cast.jadmin_iac.delete(f"/api/admin/assignments/{created['id']}", role=ADMIN, jurisdiction="IAC").status_code == 404


def test_assigning_the_same_email_twice_is_a_409(world, cast):
    body = {"email": "twice@justice.gov.uk", "jurisdiction": "IAC", "role": JUDGE}
    assert cast.jsuper.post("/api/admin/assignments", role=SUPER, json=body).status_code == 201
    again = cast.jsuper.post("/api/admin/assignments", role=SUPER, json=body)
    assert again.status_code == 409 and code(again) == "already_assigned"
