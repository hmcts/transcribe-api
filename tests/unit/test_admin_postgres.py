"""The administration rules on a real PostgreSQL (TEST_POSTGRES_URL, a throwaway one).

What SQLite would hide: the partial unique indexes and CHECK constraints of ``role_assignment``, the row
locks that make a request decided once (and requested once), a transaction that really rolls back, the
JSONB audit rows the role history reads, and the user list's SQL. Skipped without a database.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from transcribe_api.domain import access_requests, user_admin
from transcribe_api.domain import assignments as assignment_service
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.auth.auth_models_dictation import AuthenticatedUser
from transcribe_api.domain.auth.sessions import sign_in
from transcribe_api.domain.models_dictation import AccessRequest, AuditLog, Jurisdiction, RoleAssignment, User

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)"
)

JUDGE, LTM, ADMIN, SUPER = "Judge", "LegalTextManager", "JurisdictionAdministrator", "JurisdictionSuperAdministrator"
SYSTEM = "SystemAdministrator"


@pytest.fixture
def pg(fresh_postgres, monkeypatch):
    from transcribe_api.domain import audit
    from transcribe_api.runtime import db_connection

    monkeypatch.setattr(db_connection, "get_engine", lambda: fresh_postgres)
    monkeypatch.setattr(audit, "get_engine", lambda: fresh_postgres)
    return fresh_postgres


def jurisdiction(engine, code: str) -> Jurisdiction:
    with Session(engine) as session:
        row = session.exec(select(Jurisdiction).where(Jurisdiction.code == code)).first()
        if row is None:
            row = Jurisdiction(code=code, name=f"{code} name", settings={})
            session.add(row)
            session.commit()
            session.refresh(row)
        return row


def signed_up(engine, name: str, email: str | None = None) -> User:
    with Session(engine) as session:
        return sign_in(
            session, azure_user_id=str(uuid4()), email=email or f"{name}.{uuid4().hex[:5]}@justice.gov.uk", name=name
        )


def acting(user: User, role: str, code: str | None, *, sysadmin: bool = False) -> AuthenticatedUser:
    ctx = Context(code, role) if role else None
    return AuthenticatedUser(db_user=user, ctx=ctx, sysadmin=sysadmin, assignments=(ctx,) if ctx else ())


def refused_with(call) -> int:
    with pytest.raises(HTTPException) as caught:
        call()
    return caught.value.status_code


# --- assignments ----------------------------------------------------------------------------------------------


def test_the_same_assignment_made_at_the_same_moment_is_made_once_and_refused_once(pg):
    iac = jurisdiction(pg, "IAC")
    admin = acting(signed_up(pg, "Admin"), ADMIN, "IAC")
    target = signed_up(pg, "Target")
    barrier = threading.Barrier(2)

    def attempt() -> str:
        with Session(pg) as session:
            barrier.wait()
            try:
                user_admin.create_assignment(
                    session, admin, user_id=target.id, email=None, jurisdiction_code="IAC", role=JUDGE
                )
            except HTTPException as exc:
                return f"{exc.status_code} {exc.detail['code']}"
            return "created"

    with ThreadPoolExecutor(2) as pool:
        outcomes = sorted(f.result() for f in [pool.submit(attempt), pool.submit(attempt)])

    assert outcomes == ["409 already_assigned", "created"]
    with Session(pg) as session:
        assert len(session.exec(select(RoleAssignment).where(RoleAssignment.user_id == target.id)).all()) == 1
    assert iac.code == "IAC"


def test_the_databases_constraints_back_the_rules_up(pg):
    iac = jurisdiction(pg, "IAC")
    user = signed_up(pg, "U")
    for role, jurisdiction_id in (
        ("SystemAdministrator", iac.id),
        ("Normal", iac.id),
        (SUPER, iac.id),
        (ADMIN, None),
        (JUDGE, None),
    ):
        with Session(pg) as session:
            session.add(RoleAssignment(user_id=user.id, jurisdiction_id=jurisdiction_id, role=role))
            with pytest.raises(IntegrityError):
                session.commit()
    # A pending email is stored lower case, and one holder cannot hold a role twice.
    with Session(pg) as session:
        session.add(RoleAssignment(pending_email="Mixed@Case.test", jurisdiction_id=iac.id, role=JUDGE))
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_pending_assignment_is_claimed_at_the_first_sign_in_and_its_id_works_until_then(pg):
    jurisdiction(pg, "IAC")
    admin = acting(signed_up(pg, "Admin"), ADMIN, "IAC")
    with Session(pg) as session:
        view = user_admin.create_assignment(
            session, admin, user_id=None, email="Later@Justice.gov.uk", jurisdiction_code="IAC", role=JUDGE
        )
    assert view.pending and view.email == "later@justice.gov.uk"

    with Session(pg) as session:
        page = user_admin.list_users(session, admin, search="later", jurisdiction=None, role=None, page=1, page_size=10)
    (row,) = page.items
    assert row.pending and row.id == view.id

    claimed = signed_up(pg, "Later", "LATER@justice.gov.uk")
    with Session(pg) as session:
        mine = session.exec(select(RoleAssignment).where(RoleAssignment.user_id == claimed.id)).all()
        assert [(r.role, r.pending_email) for r in mine] == [(JUDGE, None)]
        assert session.get(RoleAssignment, view.id).user_id == claimed.id  # the same row, claimed
    # The id the list gave it still removes it: it is now the user's assignment.
    with Session(pg) as session:
        user_admin.delete_assignment(session, admin, view.id)
        assert session.exec(select(RoleAssignment).where(RoleAssignment.user_id == claimed.id)).all() == []


def test_the_user_list_pages_users_then_pending_rows_and_escapes_its_search(pg):
    jurisdiction(pg, "IAC")
    jsuper = acting(signed_up(pg, "Super", "super@justice.gov.uk"), SUPER, None)
    for i in range(3):
        signed_up(pg, f"U{i}", f"user{i}@justice.gov.uk")
    signed_up(pg, "Odd", "odd_100%@justice.gov.uk")
    for email in ("p1@justice.gov.uk", "p2@justice.gov.uk"):
        with Session(pg) as session:
            user_admin.create_assignment(
                session, jsuper, user_id=None, email=email, jurisdiction_code="IAC", role=ADMIN
            )

    def page(n, size, **kw):
        with Session(pg) as session:
            return user_admin.list_users(
                session,
                jsuper,
                search=kw.get("search"),
                jurisdiction=kw.get("jurisdiction"),
                role=kw.get("role"),
                page=n,
                page_size=size,
            )

    first, second, third = page(1, 3), page(2, 3), page(3, 3)
    assert first.total == 7 == second.total
    assert [i.pending for i in first.items + second.items + third.items] == [False] * 5 + [True] * 2
    assert [len(p.items) for p in (first, second, third)] == [3, 3, 1]
    # A search for "_100%" is a literal one, not a pattern.
    assert [i.email for i in page(1, 10, search="_100%").items] == ["odd_100%@justice.gov.uk"]
    assert page(1, 10, search="%").total == 1
    assert [i.email for i in page(1, 10, role=ADMIN, jurisdiction="IAC").items] == [
        "p1@justice.gov.uk",
        "p2@justice.gov.uk",
    ]


def test_the_role_history_reads_the_jurisdictions_rows_by_object_and_is_paged(pg):
    jurisdiction(pg, "IAC")
    jurisdiction(pg, "ET")
    admin_iac_user = signed_up(pg, "AdminIAC")
    admin_iac = acting(admin_iac_user, ADMIN, "IAC")
    admin_et = acting(signed_up(pg, "AdminET"), ADMIN, "ET")
    target = signed_up(pg, "Target")
    for actor, code, role in ((admin_iac, "IAC", JUDGE), (admin_et, "ET", JUDGE), (admin_iac, "IAC", LTM)):
        with Session(pg) as session:
            user_admin.create_assignment(
                session, actor, user_id=target.id, email=None, jurisdiction_code=code, role=role
            )
    with Session(pg) as session:
        mine = user_admin.role_history(session, admin_iac, jurisdiction=None, page=1, page_size=50)
        own = user_admin.role_history(session, admin_iac, jurisdiction="IAC", page=1, page_size=50)
        only_et = user_admin.role_history(session, admin_et, jurisdiction=None, page=1, page_size=50)
        paged = user_admin.role_history(session, admin_iac, jurisdiction=None, page=2, page_size=1)
        assert (
            refused_with(lambda: user_admin.role_history(session, admin_iac, jurisdiction="ET", page=1, page_size=50))
            == 403
        )
    assert [(e.jurisdiction, e.role) for e in mine.items] == [("IAC", LTM), ("IAC", JUDGE)] and mine.total == 2
    assert own.total == 2
    assert [(e.jurisdiction, e.role) for e in only_et.items] == [("ET", JUDGE)]
    assert mine.items[0].actor_email == admin_iac_user.email and mine.items[0].target_email == target.email
    assert paged.total == 2 and [(e.jurisdiction, e.role) for e in paged.items] == [("IAC", JUDGE)]


def test_one_users_role_history_is_read_by_the_json_target_on_the_real_schema(pg):
    jurisdiction(pg, "IAC")
    jurisdiction(pg, "ET")
    admin_iac = acting(signed_up(pg, "AdminIAC"), ADMIN, "IAC")
    admin_et = acting(signed_up(pg, "AdminET"), ADMIN, "ET")
    super_admin = acting(signed_up(pg, "Super"), SUPER, None)
    target = signed_up(pg, "Target")
    other = signed_up(pg, "Other")
    for actor, code, who in ((admin_iac, "IAC", target), (admin_et, "ET", target), (admin_iac, "IAC", other)):
        with Session(pg) as session:
            user_admin.create_assignment(session, actor, user_id=who.id, email=None, jurisdiction_code=code, role=JUDGE)
    with Session(pg) as session:
        user_admin.set_user_active(session, super_admin, target.id, active=False)
    with Session(pg) as session:
        everywhere = user_admin.role_history(
            session, super_admin, jurisdiction=None, user_id=target.id, page=1, page_size=50
        )
        in_iac = user_admin.role_history(session, admin_iac, jurisdiction=None, user_id=target.id, page=1, page_size=50)
    assert everywhere.total == 3
    assert sorted((e.change, e.jurisdiction or "") for e in everywhere.items) == [
        ("assignment.added", "ET"),
        ("assignment.added", "IAC"),
        ("user.deactivated", ""),
    ]
    assert {e.target_id for e in everywhere.items} == {str(target.id)}
    assert [(e.change, e.jurisdiction) for e in in_iac.items] == [("assignment.added", "IAC")]


# --- access requests ------------------------------------------------------------------------------------------------


def _request(engine, user: User, roles=(JUDGE,), code="IAC"):
    with Session(engine) as session:
        return access_requests.create_request(
            session, session.get(User, user.id), jurisdiction_code=code, roles=list(roles)
        )


def test_identical_requests_sent_at_the_same_moment_make_one_request(pg):
    jurisdiction(pg, "IAC")
    requester = signed_up(pg, "Requester")
    barrier = threading.Barrier(4)

    def attempt() -> str:
        barrier.wait()
        try:
            _request(pg, requester)
        except HTTPException as exc:
            return f"{exc.status_code} {exc.detail['code']}"
        return "created"

    with ThreadPoolExecutor(4) as pool:
        outcomes = sorted(f.result() for f in [pool.submit(attempt) for _ in range(4)])

    assert outcomes == ["409 duplicate_request"] * 3 + ["created"]
    with Session(pg) as session:
        assert len(session.exec(select(AccessRequest).where(AccessRequest.user_id == requester.id)).all()) == 1


def test_a_request_decided_at_the_same_moment_is_decided_once(pg):
    iac = jurisdiction(pg, "IAC")
    admin = acting(signed_up(pg, "Admin"), ADMIN, "IAC")
    other_admin = acting(signed_up(pg, "Admin2"), ADMIN, "IAC")
    requester = signed_up(pg, "Requester")
    created = _request(pg, requester, roles=(JUDGE, LTM))
    barrier = threading.Barrier(2)

    def decide(actor, approve) -> str:
        with Session(pg) as session:
            barrier.wait()
            try:
                access_requests.decide_request(session, actor, created.id, approve=approve, reason=None)
            except HTTPException as exc:
                return f"{exc.status_code} {exc.detail['code']}"
            return "approved" if approve else "rejected"

    with ThreadPoolExecutor(2) as pool:
        outcomes = sorted(
            f.result() for f in [pool.submit(decide, admin, True), pool.submit(decide, other_admin, False)]
        )

    assert "409 request_not_pending" in outcomes and len(outcomes) == 2
    winner = next(o for o in outcomes if not o.startswith("409"))
    with Session(pg) as session:
        stored = session.get(AccessRequest, created.id)
        held = sorted(
            r.role for r in session.exec(select(RoleAssignment).where(RoleAssignment.user_id == requester.id))
        )
    assert stored.status == winner
    assert held == ([JUDGE, LTM] if winner == "approved" else [])
    assert iac.code == "IAC"


def test_approving_is_one_transaction_a_failure_leaves_the_request_pending_and_nothing_written(pg, monkeypatch):
    iac = jurisdiction(pg, "IAC")
    admin = acting(signed_up(pg, "Admin"), ADMIN, "IAC")
    requester = signed_up(pg, "Requester")
    created = _request(pg, requester, roles=(JUDGE, LTM))
    real = assignment_service.ensure_assignment
    calls: list[str] = []

    def fail_on_the_second(session, **kwargs):
        calls.append(kwargs["role"])
        if len(calls) == 2:
            msg = "the second assignment cannot be written"
            raise RuntimeError(msg)
        return real(session, **kwargs)

    monkeypatch.setattr(assignment_service, "ensure_assignment", fail_on_the_second)
    with Session(pg) as session, pytest.raises(RuntimeError):
        access_requests.decide_request(session, admin, created.id, approve=True, reason="ok")

    with Session(pg) as session:
        stored = session.get(AccessRequest, created.id)
        assert (stored.status, stored.decided_at, stored.decided_by, stored.reason) == ("pending", None, None, None)
        assert session.exec(select(RoleAssignment).where(RoleAssignment.user_id == requester.id)).all() == []
        assert session.exec(select(AuditLog).where(AuditLog.event_type == "access_request.approved")).all() == []
    # And with the fault gone, the same request is approved.
    monkeypatch.setattr(assignment_service, "ensure_assignment", real)
    with Session(pg) as session:
        access_requests.decide_request(session, admin, created.id, approve=True, reason=None)
    with Session(pg) as session:
        assert sorted(
            r.role for r in session.exec(select(RoleAssignment).where(RoleAssignment.user_id == requester.id))
        ) == [JUDGE, LTM]
    assert iac.code == "IAC"


def test_an_assignment_made_while_a_request_is_being_approved_is_not_a_failure(pg, monkeypatch):
    """The Judge row appears between the lookup and the insert (a unique violation): the approval tries again and succeeds."""
    iac = jurisdiction(pg, "IAC")
    admin = acting(signed_up(pg, "Admin"), ADMIN, "IAC")
    requester = signed_up(pg, "Requester")
    created = _request(pg, requester, roles=(JUDGE,))
    real_find = assignment_service.find_assignment
    raced = {"done": False}

    def find_then_race(session, **kwargs):
        found = real_find(session, **kwargs)
        if not raced["done"] and found is None and kwargs.get("user_id") == requester.id:
            raced["done"] = True
            with Session(pg) as other:  # someone else assigns it right now
                other.add(RoleAssignment(user_id=requester.id, jurisdiction_id=iac.id, role=JUDGE))
                other.commit()
        return found

    monkeypatch.setattr(assignment_service, "find_assignment", find_then_race)
    with Session(pg) as session:
        decided = access_requests.decide_request(session, admin, created.id, approve=True, reason=None)

    assert decided.status == "approved" and raced["done"]
    with Session(pg) as session:
        assert [r.role for r in session.exec(select(RoleAssignment).where(RoleAssignment.user_id == requester.id))] == [
            JUDGE
        ]


def test_administrators_give_themselves_the_roles_they_appoint_on_the_real_schema_and_a_judge_cannot_assign(pg):
    jurisdiction(pg, "IAC")
    system_user = signed_up(pg, "Sys")
    system = acting(system_user, SYSTEM, None, sysadmin=True)
    with Session(pg) as session:
        own = user_admin.create_assignment(
            session, system, user_id=system_user.id, email=None, jurisdiction_code=None, role=SUPER
        )
    assert own.role == SUPER and own.jurisdiction_code is None
    with Session(pg) as session:
        row = session.get(RoleAssignment, own.id)
        assert row.jurisdiction_id is None and row.granted_by == system_user.id

    for held, held_at, given in ((SUPER, None, ADMIN), (ADMIN, "IAC", JUDGE), (ADMIN, "IAC", LTM)):
        user = signed_up(pg, held)
        with Session(pg) as session:
            made = user_admin.create_assignment(
                session, acting(user, held, held_at), user_id=user.id, email=None, jurisdiction_code="IAC", role=given
            )
        assert (made.role, made.jurisdiction_code) == (given, "IAC")

    other = signed_up(pg, "Other")
    judge = acting(signed_up(pg, "Ju"), JUDGE, "IAC")
    with Session(pg) as session:
        assert (
            refused_with(
                lambda: user_admin.create_assignment(
                    session, judge, user_id=other.id, email=None, jurisdiction_code="IAC", role=JUDGE
                )
            )
            == 403
        )


def test_a_jurisdiction_administrator_gives_itself_judge_and_legal_text_manager_on_the_real_schema(pg):
    iac = jurisdiction(pg, "IAC")
    jsuper = acting(signed_up(pg, "Super"), SUPER, None)
    admin_user = signed_up(pg, "Admin")
    with Session(pg) as session:
        user_admin.create_assignment(
            session, jsuper, user_id=admin_user.id, email=None, jurisdiction_code="IAC", role=ADMIN
        )
    admin = acting(admin_user, ADMIN, "IAC")
    made = {}
    for role in (JUDGE, LTM):
        with Session(pg) as session:
            made[role] = user_admin.create_assignment(
                session, admin, user_id=admin_user.id, email=None, jurisdiction_code="IAC", role=role
            )

    with Session(pg) as session:
        rows = session.exec(select(RoleAssignment).where(RoleAssignment.user_id == admin_user.id)).all()
        assert sorted((r.role, r.jurisdiction_id) for r in rows) == sorted(
            (role, iac.id) for role in (ADMIN, JUDGE, LTM)
        )
        assert all(r.granted_by == admin_user.id for r in rows if r.role != ADMIN)
        loaded = assignment_service.load_assignments(session, admin_user.id)
    assert sorted((a.role, a.jurisdiction_code) for a in loaded) == sorted(
        (role, "IAC") for role in (ADMIN, JUDGE, LTM)
    )
    assert all(a.id is not None for a in loaded)

    admin_row = next(r for r in rows if r.role == ADMIN)
    with Session(pg) as session:
        assert refused_with(lambda: user_admin.delete_assignment(session, admin, admin_row.id)) == 403
    with Session(pg) as session:
        user_admin.delete_assignment(session, admin, made[JUDGE].id)
    with Session(pg) as session:
        remaining = session.exec(select(RoleAssignment.role).where(RoleAssignment.user_id == admin_user.id)).all()
    assert sorted(remaining) == sorted([ADMIN, LTM])


def test_a_user_holds_roles_in_several_jurisdictions_on_the_real_schema(pg):
    for code in ("IAC", "ET", "SSCS"):
        jurisdiction(pg, code)
    jsuper = acting(signed_up(pg, "Super"), SUPER, None)
    admins = {code: acting(signed_up(pg, f"Admin{code}"), ADMIN, code) for code in ("IAC", "ET")}
    multi = signed_up(pg, "Multi")
    for actor, code, role in (
        (admins["IAC"], "IAC", JUDGE),
        (admins["ET"], "ET", JUDGE),
        (admins["ET"], "ET", LTM),
        (jsuper, "SSCS", ADMIN),
    ):
        with Session(pg) as session:
            user_admin.create_assignment(
                session, actor, user_id=multi.id, email=None, jurisdiction_code=code, role=role
            )

    everything = {("IAC", JUDGE), ("ET", JUDGE), ("ET", LTM), ("SSCS", ADMIN)}
    with Session(pg) as session:
        loaded = assignment_service.load_assignments(session, multi.id)
    assert {(a.jurisdiction_code, a.role) for a in loaded} == everything and len(loaded) == 4

    def listed(actor) -> list[tuple[str | None, str]]:
        with Session(pg) as session:
            page = user_admin.list_users(
                session, actor, search=multi.email, jurisdiction=None, role=None, page=1, page_size=10
            )
        (row,) = page.items
        return sorted((a.jurisdiction_code, a.role) for a in row.assignments)

    assert listed(admins["IAC"]) == [("IAC", JUDGE)]
    assert listed(admins["ET"]) == [("ET", JUDGE), ("ET", LTM)]
    assert listed(acting(multi, ADMIN, "SSCS")) == [("SSCS", ADMIN)]
    assert listed(jsuper) == [("SSCS", ADMIN)]

    iac_judge = next(a for a in loaded if (a.jurisdiction_code, a.role) == ("IAC", JUDGE))
    with Session(pg) as session:
        assert refused_with(lambda: user_admin.delete_assignment(session, admins["ET"], iac_judge.id)) == 403
    with Session(pg) as session:
        assert {
            (a.jurisdiction_code, a.role) for a in assignment_service.load_assignments(session, multi.id)
        } == everything


# --- feature flags -------------------------------------------------------------------------------------------------


def test_two_first_writes_of_a_feature_flag_at_the_same_moment_both_succeed(pg):
    """Review finding D6: both read 'no row' and both inserted, so one failed on the primary key (a 500)."""
    from transcribe_api.domain import feature_flags

    user = signed_up(pg, "Sysadmin")
    writers = 6
    barrier = threading.Barrier(writers)

    def attempt(value: bool) -> bool:
        with Session(pg) as session:
            barrier.wait()
            return feature_flags.set_flag(
                session, "dictation_enabled", value, updated_by=user.id, acted_as=["SystemAdministrator"]
            )

    with ThreadPoolExecutor(writers) as pool:
        results = [f.result() for f in [pool.submit(attempt, n % 2 == 0) for n in range(writers)]]

    assert len(results) == writers  # none raised
    with Session(pg) as session:
        (row,) = session.exec(select(feature_flags.SystemFeatureFlag)).all()
        assert row.value in (True, False) and row.updated_by == user.id
        audited = session.exec(select(AuditLog).where(AuditLog.event_type == "feature_flag.changed")).all()
    assert len(audited) == writers  # each change has its row, written with it
