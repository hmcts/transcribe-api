"""Role assignments (DIAAT-382; domain/assignments.py): the rules, the pending-email claim, leaving."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from tests.unit.jurisdiction_fixtures import add_jurisdiction, add_user
from transcribe_api.domain import assignments as svc
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.models_dictation import RoleAssignment

pytestmark = pytest.mark.real_jurisdictions


@pytest.fixture
def world(jurisdiction_db):
    return {
        "engine": jurisdiction_db,
        "et": add_jurisdiction(jurisdiction_db, "ET"),
        "iac": add_jurisdiction(jurisdiction_db, "IAC"),
        "off": add_jurisdiction(jurisdiction_db, "OFF", enabled=False),
        "user": add_user(jurisdiction_db),
        "other": add_user(jurisdiction_db),
    }


@pytest.fixture
def session(world):
    with Session(world["engine"]) as session:
        yield session


def test_a_user_can_hold_several_roles_in_several_jurisdictions(world, session):
    et, iac, user = world["et"], world["iac"], world["user"]
    for jurisdiction, role in [
        (et, "Judge"),
        (et, "LegalTextManager"),
        (iac, "Judge"),
        (iac, "JurisdictionAdministrator"),
    ]:
        svc.add_assignment(session, role=role, jurisdiction_id=jurisdiction.id, user_id=user.id)
    svc.add_assignment(session, role="JurisdictionSuperAdministrator", jurisdiction_id=None, user_id=user.id)
    session.commit()

    loaded = svc.load_assignments(session, user.id)

    # By jurisdiction name (global first), then role in the order of the roles.
    assert [(a.jurisdiction_code, a.role) for a in loaded] == [
        (None, "JurisdictionSuperAdministrator"),
        ("ET", "Judge"),
        ("ET", "LegalTextManager"),
        ("IAC", "Judge"),
        ("IAC", "LegalTextManager"),  # included with the administrator role, not given
        ("IAC", "JurisdictionAdministrator"),
    ]
    assert svc.distinct_contexts(loaded)[0] == Context(None, "JurisdictionSuperAdministrator")
    assert len(svc.distinct_contexts(loaded)) == 6


def test_a_role_held_in_one_jurisdiction_is_nothing_in_another(world, session):
    svc.add_assignment(session, role="JurisdictionAdministrator", jurisdiction_id=world["et"].id, user_id=world["user"].id)
    session.commit()
    contexts = svc.distinct_contexts(svc.load_assignments(session, world["user"].id))
    # What comes with the administrator role is theirs in ET too, and nothing in IAC.
    assert contexts == [
        Context("ET", "Judge"),
        Context("ET", "LegalTextManager"),
        Context("ET", "JurisdictionAdministrator"),
    ]
    assert all(context.jurisdiction != "IAC" for context in contexts)


def test_assignments_in_a_disabled_jurisdiction_are_not_loaded(world, session):
    svc.add_assignment(session, role="Judge", jurisdiction_id=world["off"].id, user_id=world["user"].id)
    svc.add_assignment(session, role="Judge", jurisdiction_id=world["et"].id, user_id=world["user"].id)
    session.commit()
    assert [a.jurisdiction_code for a in svc.load_assignments(session, world["user"].id)] == ["ET"]


def test_nobody_elses_assignments_are_loaded(world, session):
    svc.add_assignment(session, role="Judge", jurisdiction_id=world["et"].id, user_id=world["other"].id)
    session.commit()
    assert svc.load_assignments(session, world["user"].id) == []


@pytest.mark.parametrize("role", ["SystemAdministrator", "Normal", "judge", "", "Admin"])
def test_a_role_that_cannot_be_stored_is_refused(world, session, role):
    with pytest.raises(svc.AssignmentError):
        svc.add_assignment(session, role=role, jurisdiction_id=world["et"].id, user_id=world["user"].id)
    assert session.exec(select(RoleAssignment)).all() == []


@pytest.mark.parametrize("role", ["Judge", "LegalTextManager", "JurisdictionAdministrator"])
def test_a_jurisdiction_role_needs_a_jurisdiction(world, session, role):
    with pytest.raises(svc.AssignmentError, match="needs a jurisdiction"):
        svc.add_assignment(session, role=role, jurisdiction_id=None, user_id=world["user"].id)


def test_the_global_role_has_no_jurisdiction(world, session):
    with pytest.raises(svc.AssignmentError, match="global"):
        svc.add_assignment(
            session, role="JurisdictionSuperAdministrator", jurisdiction_id=world["et"].id, user_id=world["user"].id
        )


def test_an_assignment_names_a_user_or_an_email_not_both_and_not_neither(world, session):
    for kwargs in ({}, {"user_id": world["user"].id, "email": "a@example.test"}):
        with pytest.raises(svc.AssignmentError):
            svc.add_assignment(session, role="Judge", jurisdiction_id=world["et"].id, **kwargs)


def test_the_same_assignment_twice_is_a_duplicate(world, session):
    args = {"role": "Judge", "jurisdiction_id": world["et"].id, "user_id": world["user"].id}
    svc.add_assignment(session, **args)
    with pytest.raises(svc.DuplicateAssignmentError):
        svc.add_assignment(session, **args)
    glob = {"role": "JurisdictionSuperAdministrator", "jurisdiction_id": None, "user_id": world["user"].id}
    svc.add_assignment(session, **glob)
    with pytest.raises(svc.DuplicateAssignmentError):  # a NULL jurisdiction is not "different" each time
        svc.add_assignment(session, **glob)


def test_ensure_assignment_is_idempotent(world, session):
    args = {"role": "Judge", "jurisdiction_id": world["et"].id, "user_id": world["user"].id}
    first, created = svc.ensure_assignment(session, **args, granted_by=world["other"].id)
    again, created_again = svc.ensure_assignment(session, **args)
    assert (created, created_again) == (True, False) and first.id == again.id
    assert len(session.exec(select(RoleAssignment)).all()) == 1


def test_the_database_refuses_a_second_global_role_for_one_user_whatever_the_code_does(world, session):
    """The unique index, not only the service: NULL is not equal to NULL in a plain unique constraint."""
    for _ in range(2):
        session.add(RoleAssignment(user_id=world["user"].id, role="JurisdictionSuperAdministrator"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_the_database_refuses_what_the_rules_refuse(world, session):
    for row in (
        RoleAssignment(user_id=world["user"].id, role="Judge"),  # no jurisdiction
        RoleAssignment(user_id=world["user"].id, jurisdiction_id=world["et"].id, role="JurisdictionSuperAdministrator"),
        RoleAssignment(user_id=world["user"].id, jurisdiction_id=world["et"].id, role="SystemAdministrator"),
        RoleAssignment(user_id=world["user"].id, jurisdiction_id=world["et"].id, role="Normal"),
        RoleAssignment(jurisdiction_id=world["et"].id, role="Judge"),  # nobody
        RoleAssignment(pending_email="Mixed@Case.test", jurisdiction_id=world["et"].id, role="Judge"),
    ):
        with pytest.raises(IntegrityError):
            session.add(row)
            session.commit()
        session.rollback()


def test_remove_an_assignment(world, session):
    row = svc.add_assignment(session, role="Judge", jurisdiction_id=world["et"].id, user_id=world["user"].id)
    session.commit()
    assert svc.remove_assignment(session, row.id) is True
    assert svc.remove_assignment(session, row.id) is False
    assert svc.remove_assignment(session, uuid4()) is False
    session.commit()
    assert svc.load_assignments(session, world["user"].id) == []


# --- the pending email ------------------------------------------------------------------


def test_an_assignment_by_email_is_pending_in_lower_case(world, session):
    row = svc.add_assignment(session, role="Judge", jurisdiction_id=world["et"].id, email="  Judge@Example.TEST ")
    assert row.user_id is None and row.pending_email == "judge@example.test"
    with pytest.raises(svc.DuplicateAssignmentError):
        svc.add_assignment(session, role="Judge", jurisdiction_id=world["et"].id, email="JUDGE@example.test")


@pytest.mark.parametrize("signing_in_as", ["judge@example.test", "JUDGE@EXAMPLE.TEST", " Judge@Example.Test  "])
def test_the_pending_email_is_claimed_whatever_the_case(world, session, signing_in_as):
    svc.add_assignment(session, role="Judge", jurisdiction_id=world["et"].id, email="judge@example.test")
    svc.add_assignment(session, role="LegalTextManager", jurisdiction_id=world["iac"].id, email="judge@example.test")
    svc.add_assignment(session, role="JurisdictionSuperAdministrator", jurisdiction_id=None, email="judge@example.test")
    svc.add_assignment(session, role="Judge", jurisdiction_id=world["iac"].id, email="someone.else@example.test")
    session.commit()

    claimed = svc.claim_pending_assignments(session, world["user"].id, signing_in_as)
    session.commit()

    assert claimed == 3
    mine = svc.load_assignments(session, world["user"].id)
    assert [(a.jurisdiction_code, a.role) for a in mine] == [
        (None, "JurisdictionSuperAdministrator"),
        ("ET", "Judge"),
        ("IAC", "LegalTextManager"),
    ]
    rows = session.exec(select(RoleAssignment)).all()
    assert [r.pending_email for r in rows if r.user_id == world["user"].id] == [None, None, None]
    # Somebody else's is still waiting for them.
    assert [r.pending_email for r in rows if r.user_id is None] == ["someone.else@example.test"]


def test_claiming_twice_claims_nothing_the_second_time(world, session):
    svc.add_assignment(session, role="Judge", jurisdiction_id=world["et"].id, email="judge@example.test")
    assert svc.claim_pending_assignments(session, world["user"].id, "judge@example.test") == 1
    assert svc.claim_pending_assignments(session, world["user"].id, "judge@example.test") == 0


def test_a_pending_assignment_for_something_already_held_is_dropped_not_claimed_twice(world, session):
    svc.add_assignment(session, role="Judge", jurisdiction_id=world["et"].id, user_id=world["user"].id)
    svc.add_assignment(session, role="Judge", jurisdiction_id=world["et"].id, email="judge@example.test")
    session.commit()

    assert svc.claim_pending_assignments(session, world["user"].id, "judge@example.test") == 0
    session.commit()
    assert len(session.exec(select(RoleAssignment)).all()) == 1


@pytest.mark.parametrize("email", [None, "", "   "])
def test_no_email_claims_nothing(world, session, email):
    svc.add_assignment(session, role="Judge", jurisdiction_id=world["et"].id, email="judge@example.test")
    assert svc.claim_pending_assignments(session, world["user"].id, email) == 0


# --- leaving ----------------------------------------------------------------------------


def test_leaving_removes_all_the_users_roles_there_and_nothing_else(world, session):
    et, iac, user, other = world["et"], world["iac"], world["user"], world["other"]
    for who in (user, other):
        svc.add_assignment(session, role="Judge", jurisdiction_id=et.id, user_id=who.id)
        svc.add_assignment(session, role="LegalTextManager", jurisdiction_id=et.id, user_id=who.id)
        svc.add_assignment(session, role="Judge", jurisdiction_id=iac.id, user_id=who.id)
    svc.add_assignment(session, role="JurisdictionSuperAdministrator", jurisdiction_id=None, user_id=user.id)
    svc.add_assignment(session, role="Judge", jurisdiction_id=et.id, email="pending@example.test")
    session.commit()

    left = svc.leave_jurisdiction(session, user.id, " et ")
    session.commit()

    assert left.jurisdiction_id == et.id and left.roles == ["Judge", "LegalTextManager"]
    mine = [(a.jurisdiction_code, a.role) for a in svc.load_assignments(session, user.id)]
    assert mine == [(None, "JurisdictionSuperAdministrator"), ("IAC", "Judge")]
    assert len(svc.load_assignments(session, other.id)) == 3
    assert len(session.exec(select(RoleAssignment).where(RoleAssignment.pending_email.is_not(None))).all()) == 1


def test_leaving_a_jurisdiction_you_hold_nothing_in_removes_nothing(world, session):
    svc.add_assignment(session, role="Judge", jurisdiction_id=world["et"].id, user_id=world["user"].id)
    session.commit()
    assert svc.leave_jurisdiction(session, world["user"].id, "IAC").roles == []
    assert svc.leave_jurisdiction(session, world["user"].id, "NOPE") == svc.Left(None, [])
    assert len(svc.load_assignments(session, world["user"].id)) == 1


# --- Judge and LegalTextManager come with the administrator role (DIAAT-373) -------------


def _pairs(assignments):
    return [(a.jurisdiction_code, a.role, a.derived) for a in assignments]


def test_an_administrator_also_holds_judge_and_legal_text_manager_in_their_own_jurisdiction(world, session):
    svc.add_assignment(session, role="JurisdictionAdministrator", jurisdiction_id=world["iac"].id, user_id=world["user"].id)
    session.commit()

    loaded = svc.load_assignments(session, world["user"].id)

    assert _pairs(loaded) == [
        ("IAC", "Judge", True),
        ("IAC", "LegalTextManager", True),
        ("IAC", "JurisdictionAdministrator", False),
    ]
    assert all(a.id is None for a in loaded if a.derived)
    # Only the one given is what they land in when they have not chosen.
    assert svc.stored_contexts(loaded) == [Context("IAC", "JurisdictionAdministrator")]


def test_the_included_roles_are_not_stored(world, session):
    svc.add_assignment(session, role="JurisdictionAdministrator", jurisdiction_id=world["iac"].id, user_id=world["user"].id)
    session.commit()
    svc.load_assignments(session, world["user"].id)
    session.commit()
    roles = session.exec(select(RoleAssignment.role).where(RoleAssignment.user_id == world["user"].id)).all()
    assert roles == ["JurisdictionAdministrator"]


def test_the_included_roles_are_only_in_the_administrators_own_jurisdiction(world, session):
    svc.add_assignment(session, role="JurisdictionAdministrator", jurisdiction_id=world["iac"].id, user_id=world["user"].id)
    svc.add_assignment(session, role="Judge", jurisdiction_id=world["et"].id, user_id=world["user"].id)
    session.commit()

    contexts = svc.distinct_contexts(svc.load_assignments(session, world["user"].id))

    assert Context("ET", "Judge") in contexts
    assert Context("ET", "LegalTextManager") not in contexts
    assert Context("ET", "JurisdictionAdministrator") not in contexts
    assert Context("IAC", "LegalTextManager") in contexts


def test_a_role_held_as_a_row_is_not_listed_twice(world, session):
    svc.add_assignment(session, role="JurisdictionAdministrator", jurisdiction_id=world["iac"].id, user_id=world["user"].id)
    svc.add_assignment(session, role="Judge", jurisdiction_id=world["iac"].id, user_id=world["user"].id)
    session.commit()

    loaded = svc.load_assignments(session, world["user"].id)

    assert _pairs(loaded) == [
        ("IAC", "Judge", False),
        ("IAC", "LegalTextManager", True),
        ("IAC", "JurisdictionAdministrator", False),
    ]


def test_nothing_is_included_in_a_disabled_jurisdiction(world, session):
    svc.add_assignment(session, role="JurisdictionAdministrator", jurisdiction_id=world["off"].id, user_id=world["user"].id)
    session.commit()
    assert svc.load_assignments(session, world["user"].id) == []


def test_the_super_administrator_and_other_roles_include_nothing(world, session):
    svc.add_assignment(session, role="JurisdictionSuperAdministrator", jurisdiction_id=None, user_id=world["user"].id)
    svc.add_assignment(session, role="LegalTextManager", jurisdiction_id=world["et"].id, user_id=world["other"].id)
    session.commit()
    assert _pairs(svc.load_assignments(session, world["user"].id)) == [(None, "JurisdictionSuperAdministrator", False)]
    assert _pairs(svc.load_assignments(session, world["other"].id)) == [("ET", "LegalTextManager", False)]


def test_leaving_the_jurisdiction_takes_the_included_roles_with_the_administrator_role(world, session):
    svc.add_assignment(session, role="JurisdictionAdministrator", jurisdiction_id=world["iac"].id, user_id=world["user"].id)
    session.commit()

    left = svc.leave_jurisdiction(session, world["user"].id, "IAC")
    session.commit()

    assert left.roles == ["JurisdictionAdministrator"]
    assert svc.load_assignments(session, world["user"].id) == []
