"""Migration 0010 (DIAAT-402): the DIAAT-373 cutover and the retirement of the old access data, on a real
PostgreSQL. A database with memberships is migrated through 0009, then sees what happened after it: the
previous release's pods writing memberships, and administrators and users removing roles in the new code.
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session

from alembic import command
from transcribe_api.domain.audit import stage_audit
from transcribe_api.domain.models_dictation import Jurisdiction
from transcribe_api.domain.user_admin import CHANGE_ASSIGNMENT_REMOVED, CHANGE_MEMBERSHIP_LEAVE, audit_role_change
from transcribe_api.runtime import migrations

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)"
)

PEOPLE = ("alice", "bob", "carol", "dave", "erin", "frank", "george", "henry", "admin")


@pytest.fixture
def engine():
    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    try:
        yield engine
    finally:
        engine.dispose()


def migrate(engine, revision: str, *, down: bool = False) -> None:
    with engine.begin() as connection:
        config = migrations.alembic_config()
        config.attributes["connection"] = connection
        (command.downgrade if down else command.upgrade)(config, revision)


def run(engine, sql: str, **params) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params)


def add_user(engine, name: str) -> str:
    uid = str(uuid4())
    run(
        engine,
        'INSERT INTO "user" (id, created_datetime, email, azure_user_id, has_completed_onboarding, role)'
        " VALUES (:uid, now(), :email, :oid, true, 'Judge')",
        uid=uid,
        email=f"{name.capitalize()}@Example.TEST",
        oid=f"oid-{name}",
    )
    return uid


def membership(engine, code: str, role: str, *, user: str | None = None, email: str | None = None, tester=False):
    run(
        engine,
        "INSERT INTO jurisdiction_membership (id, jurisdiction_id, user_id, email, role, tester, created_datetime)"
        " SELECT gen_random_uuid(), j.id, :uid, :email, :role, :tester, now() FROM jurisdiction j WHERE j.code = :code",
        uid=user,
        email=email,
        role=role,
        tester=tester,
        code=code,
    )


def jurisdiction(engine, code: str):
    with engine.connect() as connection:
        return connection.execute(text("SELECT id, code FROM jurisdiction WHERE code = :code"), {"code": code}).one()


def remove_assignment(engine, actor: str, code: str, role: str, *, user: str | None = None, email: str | None = None):
    """What the admin screens do (user_admin.delete_assignment): delete the row and audit it, together."""
    where = "a.user_id = :who" if user else "a.user_id IS NULL AND a.pending_email = :who"
    with Session(engine) as session:
        session.execute(
            text(
                f"DELETE FROM role_assignment a USING jurisdiction j WHERE a.jurisdiction_id = j.id"
                f" AND j.code = :code AND a.role = :role AND {where}"
            ),
            {"code": code, "role": role, "who": user or email},
        )
        audit_role_change(
            session,
            CHANGE_ASSIGNMENT_REMOVED,
            SimpleNamespace(id=UUID(actor)),
            jurisdiction=jurisdiction(engine, code),
            target_user_id=UUID(user) if user else None,
            target_email=email,
            role=role,
        )
        session.commit()


def leave(engine, user: str, code: str) -> None:
    """What routes_session.leave does."""
    j = jurisdiction(engine, code)
    with Session(engine) as session:
        session.execute(
            text("DELETE FROM role_assignment WHERE user_id = :uid AND jurisdiction_id = :jid"),
            {"uid": user, "jid": j.id},
        )
        stage_audit(
            session,
            CHANGE_MEMBERSHIP_LEAVE,
            actor_id=UUID(user),
            outcome="success",
            object_type="jurisdiction",
            object_id=j.id,
            detail={"code": code, "roles": ["Judge"]},
        )
        session.commit()


def assignments(engine) -> list[tuple]:
    """(who, jurisdiction, role, pending, tester), sorted."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT coalesce(lower(u.email), a.pending_email), j.code, a.role, a.user_id IS NULL, a.tester"
                ' FROM role_assignment a LEFT JOIN "user" u ON u.id = a.user_id'
                " JOIN jurisdiction j ON j.id = a.jurisdiction_id ORDER BY 1, 2, 3"
            )
        ).all()
    return [tuple(r) for r in rows]


def tables(engine) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"))
            .scalars()
            .all()
        )


@pytest.fixture
def world(engine):
    """Memberships before 0009, 0009, then what happened while and after the new release rolled out."""
    migrate(engine, "0008")
    run(
        engine,
        "INSERT INTO jurisdiction (id, code, name, enabled, auto_enrol, settings, created_datetime, updated_datetime)"
        " VALUES (gen_random_uuid(), 'ET', 'Employment Tribunal', true, false, '{}', now(), now())",
    )
    ids = {name: add_user(engine, name) for name in PEOPLE}

    membership(engine, "IAC", "judge", user=ids["alice"])
    membership(engine, "ET", "admin", user=ids["bob"])
    membership(engine, "ET", "judge", user=ids["carol"])
    membership(engine, "ET", "judge", email="Pending.Removed@Example.TEST")
    membership(engine, "IAC", "judge", email="Dave@Example.TEST")
    membership(engine, "ET", "judge", user=ids["erin"])
    membership(engine, "IAC", "judge", email="frank@example.test")
    membership(engine, "IAC", "admin", user=ids["admin"])

    migrate(engine, "0009")

    # The previous release's pods, still serving after 0009 ran.
    membership(engine, "ET", "judge", user=ids["alice"])  # an administrator added a judge
    membership(engine, "IAC", "admin", user=ids["bob"])  # ... and an administrator, elsewhere
    membership(engine, "IAC", "judge", user=ids["george"])  # a first sign-in, auto-enrolled in the IAC
    membership(engine, "IAC", "judge", email="New.Judge@Example.TEST")  # added by email, mixed case
    membership(engine, "IAC", "judge", email="  new.judge@example.test")  # the same, padded
    membership(engine, "ET", "admin", email="")  # no user, no email: nothing to give
    run(
        engine,
        "UPDATE jurisdiction_membership SET user_id = :uid, email = NULL WHERE email = 'Dave@Example.TEST'",
        uid=ids["dave"],
    )
    run(
        engine,
        "UPDATE jurisdiction_membership SET user_id = :uid, email = NULL WHERE email = 'frank@example.test'",
        uid=ids["frank"],
    )
    run(engine, "UPDATE jurisdiction_membership SET tester = true WHERE user_id = :uid", uid=ids["erin"])

    # The new code: an administrator removes roles 0009 copied, a user leaves, another role is given.
    remove_assignment(engine, ids["admin"], "ET", "JurisdictionAdministrator", user=ids["bob"])
    remove_assignment(engine, ids["admin"], "ET", "Judge", email="pending.removed@example.test")
    remove_assignment(engine, ids["admin"], "IAC", "Judge", email="frank@example.test")
    leave(engine, ids["carol"], "ET")
    run(
        engine,
        "INSERT INTO role_assignment (id, user_id, jurisdiction_id, role, granted_by, granted_at)"
        " SELECT gen_random_uuid(), :uid, j.id, 'LegalTextManager', :admin, now() FROM jurisdiction j WHERE j.code = 'ET'",
        uid=ids["henry"],
        admin=ids["admin"],
    )
    engine.ids = ids  # type: ignore[attr-defined]
    return engine


AFTER_0010 = [
    ("admin@example.test", "IAC", "JurisdictionAdministrator", False, False),
    ("alice@example.test", "ET", "Judge", False, False),  # the gap
    ("alice@example.test", "IAC", "Judge", False, False),
    ("bob@example.test", "IAC", "JurisdictionAdministrator", False, False),  # the gap; ET's removal stands
    ("dave@example.test", "IAC", "Judge", False, False),  # claimed by the old release: his now, no pending copy
    ("erin@example.test", "ET", "Judge", False, True),  # put in the test group by the old release
    ("george@example.test", "IAC", "Judge", False, False),  # auto-enrolled by the old release
    ("henry@example.test", "ET", "LegalTextManager", False, False),  # the new code's own, untouched
    ("new.judge@example.test", "IAC", "Judge", True, False),  # pending, lower case, trimmed, once
]


def test_0010_catches_up_the_gap_without_giving_back_what_was_removed_since_0009(world, caplog):
    before = assignments(world)
    assert ("bob@example.test", "ET", "JurisdictionAdministrator", False, False) not in before
    assert ("dave@example.test", "IAC", "Judge", True, False) in before  # 0009's copy, pending

    with caplog.at_level(logging.INFO, logger="alembic.runtime.migration"):
        migrate(world, "0010")

    # Not given back: bob in the ET, carol (left the ET), the removed pending email, frank (whose pending copy
    # was removed after the old release claimed his membership).
    assert assignments(world) == AFTER_0010
    log = caplog.text
    assert "14 membership(s)" in log
    assert "assignments added for users: 4" in log  # alice ET, bob IAC, george, dave
    assert "assignments added pending by email: 1" in log
    assert "claimed pending assignments removed: 1" in log
    assert "assignments put in the test group: 1" in log
    assert "jurisdictions with auto_enrol turned off: 1" in log
    assert "left unmatched: 4 whose role was removed since 0009, 1 other" in log
    assert "memberships with no assignment (ids)" in log


def test_0010_retires_the_membership_table_and_auto_enrol(world):
    migrate(world, "0010")

    assert "jurisdiction_membership" not in tables(world)
    assert "jurisdiction_membership_legacy" in tables(world)
    with world.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM jurisdiction_membership_legacy")).scalar_one() == 14
        assert connection.execute(text("SELECT bool_or(auto_enrol) FROM jurisdiction")).scalar_one() is False
    # The models no longer map auto_enrol: a jurisdiction they insert gets the column's default.
    with Session(world) as session:
        session.add(Jurisdiction(code="SSCS", name="Social Security", settings={}))
        session.commit()
    with world.connect() as connection:
        assert connection.execute(text("SELECT auto_enrol FROM jurisdiction WHERE code = 'SSCS'")).scalar_one() is False


def test_0010_goes_down_and_up_again_without_adding_anything(world):
    migrate(world, "0010")
    after = assignments(world)

    migrate(world, "0009", down=True)
    assert "jurisdiction_membership" in tables(world)
    assert "jurisdiction_membership_legacy" not in tables(world)
    assert assignments(world) == after  # what it added stays: the 0009 release reads assignments
    with world.connect() as connection:
        default = connection.execute(
            text(
                "SELECT column_default FROM information_schema.columns WHERE table_name = 'jurisdiction' AND column_name = 'auto_enrol'"
            )
        ).scalar_one()
    assert default is None

    migrate(world, "0010")
    assert assignments(world) == after


def test_0010_on_a_database_with_nothing_to_catch_up(engine, caplog):
    with caplog.at_level(logging.INFO, logger="alembic.runtime.migration"):
        migrate(engine, "head")
    assert "0 membership(s)" in caplog.text
    assert "memberships with no assignment" not in caplog.text
    assert "jurisdiction_membership_legacy" in tables(engine)
