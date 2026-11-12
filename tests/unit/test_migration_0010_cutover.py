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
        migrate(engine, "0010")
    assert "0 membership(s)" in caplog.text
    assert "memberships with no assignment" not in caplog.text
    assert "jurisdiction_membership_legacy" in tables(engine)


def claim_at_first_sign_in(engine, user: str, email: str) -> None:
    """What the new code does at a first sign-in (assignments.claim_pending_assignments)."""
    run(
        engine,
        "UPDATE role_assignment SET user_id = :uid, pending_email = NULL WHERE pending_email = :email",
        uid=user,
        email=email,
    )


def test_0010_follows_a_pending_membership_to_the_user_who_signed_in_since(engine, caplog):
    """A pending membership whose person has since signed in with the new code is theirs: what they claimed
    is not offered again by email, what was taken from them after the claim is not given back, and a gap
    membership is given to them directly (they will not sign in for the first time again)."""
    migrate(engine, "0008")
    run(
        engine,
        "INSERT INTO jurisdiction (id, code, name, enabled, auto_enrol, settings, created_datetime, updated_datetime)"
        " VALUES (gen_random_uuid(), 'ET', 'Employment Tribunal', true, false, '{}', now(), now())",
    )
    admin = add_user(engine, "admin")
    membership(engine, "ET", "judge", email="Ivy@Example.TEST")
    membership(engine, "ET", "judge", email="Jack@Example.TEST")
    membership(engine, "ET", "judge", email="kate@example.test")
    membership(engine, "ET", "judge", email="  Mia@Example.TEST", tester=True)
    membership(engine, "ET", "judge", email=" nora@example.test")

    migrate(engine, "0009")

    ivy, jack, kate = (add_user(engine, name) for name in ("ivy", "jack", "kate"))
    claim_at_first_sign_in(engine, ivy, "ivy@example.test")
    claim_at_first_sign_in(engine, jack, "jack@example.test")
    claim_at_first_sign_in(engine, kate, "kate@example.test")
    remove_assignment(engine, admin, "ET", "Judge", user=ivy)
    leave(engine, jack, "ET")
    remove_assignment(engine, admin, "ET", "Judge", email=" nora@example.test")  # as 0009 stored it
    liam = add_user(engine, "liam")  # signed in through the new code: nothing to claim yet
    membership(engine, "IAC", "judge", email="Liam@Example.TEST")  # ... then an old pod added him by email
    add_user(engine, "olga")
    run(
        engine,
        'INSERT INTO "user" (id, created_datetime, email, azure_user_id, has_completed_onboarding, role)'
        " VALUES (gen_random_uuid(), now(), 'OLGA@example.test', 'oid-olga-2', true, 'Judge')",
    )
    membership(engine, "IAC", "judge", email="olga@example.test")  # two accounts with that email
    assert liam

    with caplog.at_level(logging.INFO, logger="alembic.runtime.migration"):
        migrate(engine, "0010")

    assert assignments(engine) == [
        ("kate@example.test", "ET", "Judge", False, False),  # claimed: no second, pending, copy
        ("liam@example.test", "IAC", "Judge", False, False),  # his directly
        ("mia@example.test", "ET", "Judge", True, True),  # trimmed, so it can be claimed
    ]
    log = caplog.text
    assert "pending emails trimmed: 1" in log
    assert "assignments added for users: 1" in log and "assignments added pending by email: 0" in log
    assert "left unmatched: 3 whose role was removed since 0009, 1 other" in log  # ivy, jack, nora; olga


def test_0010_merges_pending_copies_that_differ_only_in_padding(engine):
    migrate(engine, "0009")
    for email, tester in (("pat@example.test", False), (" pat@example.test", True), ("pat@example.test  ", False)):
        run(
            engine,
            "INSERT INTO role_assignment (id, pending_email, jurisdiction_id, role, granted_at, tester)"
            " SELECT gen_random_uuid(), :email, id, 'Judge', now(), :tester FROM jurisdiction WHERE code = 'IAC'",
            email=email,
            tester=tester,
        )

    migrate(engine, "0010")

    assert assignments(engine) == [("pat@example.test", "IAC", "Judge", True, True)]


def _with_et(engine) -> None:
    run(
        engine,
        "INSERT INTO jurisdiction (id, code, name, enabled, auto_enrol, settings, created_datetime, updated_datetime)"
        " VALUES (gen_random_uuid(), 'ET', 'Employment Tribunal', true, false, '{}', now(), now())",
    )


def test_0010_gives_a_blank_email_to_nobody_not_even_a_user_who_signed_in_without_one(engine):
    migrate(engine, "0009")
    run(
        engine,
        'INSERT INTO "user" (id, created_datetime, email, azure_user_id, has_completed_onboarding)'
        " VALUES (gen_random_uuid(), now(), '', 'oid-no-email', false)",
    )
    membership(engine, "IAC", "admin", email="  ")

    migrate(engine, "0010")

    assert assignments(engine) == []


def test_0010_does_not_make_a_padded_copy_claimable_when_its_twin_was_removed(engine):
    """0009 lowered but did not trim: `pat@` and ` pat@` were two pending copies. An administrator removed
    the first; trimming the second would give it back."""
    migrate(engine, "0008")
    _with_et(engine)
    admin = add_user(engine, "admin")
    membership(engine, "ET", "judge", email="Pat@Example.TEST")
    membership(engine, "ET", "judge", email=" pat@example.test")
    migrate(engine, "0009")
    assert [a[0] for a in assignments(engine)] == [" pat@example.test", "pat@example.test"]

    remove_assignment(engine, admin, "ET", "Judge", email="pat@example.test")
    migrate(engine, "0010")

    assert assignments(engine) == []


def test_0010_leaves_a_pending_copy_whose_email_several_users_have(engine):
    """Which account the email is is unknown, so the pending copy is not taken as claimed by either."""
    migrate(engine, "0008")
    membership(engine, "IAC", "judge", email="olga@example.test")
    migrate(engine, "0009")
    olga = add_user(engine, "olga")
    run(
        engine,
        'INSERT INTO "user" (id, created_datetime, email, azure_user_id, has_completed_onboarding)'
        " VALUES (gen_random_uuid(), now(), 'OLGA@example.test', 'oid-olga-2', false)",
    )
    run(
        engine,
        "INSERT INTO role_assignment (id, user_id, jurisdiction_id, role, granted_at)"
        " SELECT gen_random_uuid(), :uid, id, 'Judge', now() FROM jurisdiction WHERE code = 'IAC'",
        uid=olga,
    )

    migrate(engine, "0010")

    assert ("olga@example.test", "IAC", "Judge", True, False) in assignments(engine)


def test_0010_waits_for_a_sign_in_in_progress_and_then_sees_its_user(engine):
    """A pod still on the 0009 release signs someone in while 0010 runs. Without waiting, 0010 would see no
    user, add a pending assignment, and the sign-in (already past its claim) would never pick it up."""
    import threading

    migrate(engine, "0008")
    membership(engine, "IAC", "judge", email="quinn@example.test")
    migrate(engine, "0009")
    run(engine, "DELETE FROM role_assignment")  # as if the old pods added the membership after 0009: the gap

    sign_in = engine.connect()
    transaction = sign_in.begin()
    quinn = str(uuid4())
    sign_in.execute(
        text(
            'INSERT INTO "user" (id, created_datetime, email, azure_user_id, has_completed_onboarding)'
            " VALUES (:uid, now(), 'Quinn@Example.TEST', 'oid-quinn', false)"
        ),
        {"uid": quinn},
    )
    errors: list[BaseException] = []

    def upgrade():
        try:
            migrate(engine, "0010")
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=upgrade)
    worker.start()
    worker.join(timeout=2)
    assert worker.is_alive()  # held by the sign-in's uncommitted user row
    transaction.commit()
    sign_in.close()
    worker.join(timeout=30)

    assert not worker.is_alive() and errors == []
    assert assignments(engine) == [("quinn@example.test", "IAC", "Judge", False, False)]


def test_0010_lets_a_removal_already_reading_the_jurisdictions_finish_and_honours_it(engine):
    """A pod's removal has read the jurisdiction (as delete_assignment does) when 0010 starts, then deletes
    the assignment. Had 0010 locked role_assignment first and waited for the jurisdiction afterwards, each
    would wait for the other. It takes the jurisdiction first, gives way while the removal finishes, and
    then does not give the removed role back."""
    import threading

    migrate(engine, "0008")
    membership(engine, "IAC", "judge", email="rose@example.test")
    migrate(engine, "0009")
    rose = add_user(engine, "rose")
    claim_at_first_sign_in(engine, rose, "rose@example.test")
    iac = jurisdiction(engine, "IAC")

    removal = engine.connect()
    transaction = removal.begin()
    removal.execute(text("SELECT code FROM jurisdiction WHERE id = :id"), {"id": iac.id})
    errors: list[BaseException] = []

    def upgrade():
        try:
            migrate(engine, "0010")
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=upgrade)
    worker.start()
    worker.join(timeout=1.5)
    assert worker.is_alive()  # giving way to the removal's read
    removal.execute(text("DELETE FROM role_assignment WHERE user_id = :uid"), {"uid": rose})
    removal.execute(
        text(
            "INSERT INTO audit_log (id, timestamp, event_type, user_id, held_roles, required_roles, outcome,"
            " object_type, object_id, detail) VALUES (gen_random_uuid(), now(), 'assignment.removed', :actor, '[]',"
            " '[]', 'success', 'jurisdiction', :jid, CAST(:detail AS jsonb))"
        ),
        {"actor": rose, "jid": str(iac.id), "detail": f'{{"target_user_id": "{rose}", "role": "Judge"}}'},
    )
    transaction.commit()
    removal.close()
    worker.join(timeout=60)

    assert not worker.is_alive() and errors == []
    assert assignments(engine) == []
