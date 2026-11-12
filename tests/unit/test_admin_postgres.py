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
        return sign_in(session, azure_user_id=str(uuid4()), email=email or f"{name}.{uuid4().hex[:5]}@justice.gov.uk", name=name)


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
    jsuper = acting(signed_up(pg, "Super"), SUPER, None)
    target = signed_up(pg, "Target")
    barrier = threading.Barrier(2)

    def attempt() -> str:
        with Session(pg) as session:
            barrier.wait()
            try:
                user_admin.create_assignment(session, jsuper, user_id=target.id, email=None, jurisdiction_code="IAC", role=JUDGE)
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
    for role, jurisdiction_id in (("SystemAdministrator", iac.id), ("Normal", iac.id), (SUPER, iac.id), (JUDGE, None)):
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
    jsuper = acting(signed_up(pg, "Super"), SUPER, None)
    with Session(pg) as session:
        view = user_admin.create_assignment(
            session, jsuper, user_id=None, email="Later@Justice.gov.uk", jurisdiction_code="IAC", role=JUDGE
        )
    assert view.pending and view.email == "later@justice.gov.uk"

    with Session(pg) as session:
        page = user_admin.list_users(session, jsuper, search="later", jurisdiction=None, role=None, page=1, page_size=10)
    (row,) = page.items
    assert row.pending and row.id == view.id

    claimed = signed_up(pg, "Later", "LATER@justice.gov.uk")
    with Session(pg) as session:
        mine = session.exec(select(RoleAssignment).where(RoleAssignment.user_id == claimed.id)).all()
        assert [(r.role, r.pending_email) for r in mine] == [(JUDGE, None)]
        assert session.get(RoleAssignment, view.id).user_id == claimed.id  # the same row, claimed
    # The id the list gave it still removes it: it is now the user's assignment.
    with Session(pg) as session:
        user_admin.delete_assignment(session, jsuper, view.id)
        assert session.exec(select(RoleAssignment).where(RoleAssignment.user_id == claimed.id)).all() == []


def test_the_user_list_pages_users_then_pending_rows_and_escapes_its_search(pg):
    jurisdiction(pg, "IAC")
    jsuper = acting(signed_up(pg, "Super", "super@justice.gov.uk"), SUPER, None)
    for i in range(3):
        signed_up(pg, f"U{i}", f"user{i}@justice.gov.uk")
    signed_up(pg, "Odd", "odd_100%@justice.gov.uk")
    for email in ("p1@justice.gov.uk", "p2@justice.gov.uk"):
        with Session(pg) as session:
            user_admin.create_assignment(session, jsuper, user_id=None, email=email, jurisdiction_code="IAC", role=JUDGE)

    def page(n, size, **kw):
        with Session(pg) as session:
            return user_admin.list_users(
                session, jsuper, search=kw.get("search"), jurisdiction=kw.get("jurisdiction"), role=kw.get("role"), page=n, page_size=size
            )

    first, second, third = page(1, 3), page(2, 3), page(3, 3)
    assert first.total == 7 == second.total
    assert [i.pending for i in first.items + second.items + third.items] == [False] * 5 + [True] * 2
    assert [len(p.items) for p in (first, second, third)] == [3, 3, 1]
    # A search for "_100%" is a literal one, not a pattern.
    assert [i.email for i in page(1, 10, search="_100%").items] == ["odd_100%@justice.gov.uk"]
    assert page(1, 10, search="%").total == 1
    assert [i.email for i in page(1, 10, role=JUDGE, jurisdiction="IAC").items] == ["p1@justice.gov.uk", "p2@justice.gov.uk"]


def test_the_role_history_reads_the_jurisdictions_rows_by_object_and_is_paged(pg):
    jurisdiction(pg, "IAC")
    jurisdiction(pg, "ET")
    jsuper_user = signed_up(pg, "Super")
    jsuper = acting(jsuper_user, SUPER, None)
    admin_iac = acting(signed_up(pg, "AdminIAC"), ADMIN, "IAC")
    target = signed_up(pg, "Target")
    for code, role in (("IAC", JUDGE), ("ET", JUDGE), ("IAC", LTM)):
        with Session(pg) as session:
            user_admin.create_assignment(session, jsuper, user_id=target.id, email=None, jurisdiction_code=code, role=role)
    with Session(pg) as session:
        everything = user_admin.role_history(session, jsuper, jurisdiction=None, page=1, page_size=50)
        mine = user_admin.role_history(session, admin_iac, jurisdiction=None, page=1, page_size=50)
        only_et = user_admin.role_history(session, jsuper, jurisdiction="ET", page=1, page_size=50)
        paged = user_admin.role_history(session, jsuper, jurisdiction=None, page=2, page_size=2)
    assert everything.total == 3 and [(e.jurisdiction, e.role) for e in everything.items] == [("IAC", LTM), ("ET", JUDGE), ("IAC", JUDGE)]
    assert [(e.jurisdiction, e.role) for e in mine.items] == [("IAC", LTM), ("IAC", JUDGE)] and mine.total == 2
    assert [e.jurisdiction for e in only_et.items] == ["ET"]
    assert everything.items[0].actor_email == jsuper_user.email and everything.items[0].target_email == target.email
    assert paged.total == 3 and [(e.jurisdiction, e.role) for e in paged.items] == [("IAC", JUDGE)]


# --- access requests ------------------------------------------------------------------------------------------------


def _request(engine, user: User, roles=(JUDGE,), code="IAC"):
    with Session(engine) as session:
        return access_requests.create_request(session, session.get(User, user.id), jurisdiction_code=code, roles=list(roles))


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
        outcomes = sorted(f.result() for f in [pool.submit(decide, admin, True), pool.submit(decide, other_admin, False)])

    assert "409 request_not_pending" in outcomes and len(outcomes) == 2
    winner = next(o for o in outcomes if not o.startswith("409"))
    with Session(pg) as session:
        stored = session.get(AccessRequest, created.id)
        held = sorted(r.role for r in session.exec(select(RoleAssignment).where(RoleAssignment.user_id == requester.id)))
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
        assert sorted(r.role for r in session.exec(select(RoleAssignment).where(RoleAssignment.user_id == requester.id))) == [JUDGE, LTM]
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
        assert [r.role for r in session.exec(select(RoleAssignment).where(RoleAssignment.user_id == requester.id))] == [JUDGE]


def test_a_system_administrator_gives_themselves_the_global_role_on_the_real_schema_and_a_judge_cannot_assign(pg):
    jurisdiction(pg, "IAC")
    system_user = signed_up(pg, "Sys")
    system = acting(system_user, None, None, sysadmin=True)
    with Session(pg) as session:
        own = user_admin.create_assignment(session, system, user_id=system_user.id, email=None, jurisdiction_code=None, role=SUPER)
    assert own.role == SUPER and own.jurisdiction_code is None
    with Session(pg) as session:
        row = session.get(RoleAssignment, own.id)
        assert row.jurisdiction_id is None and row.granted_by == system_user.id

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
