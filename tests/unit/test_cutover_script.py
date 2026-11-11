"""scripts-cutover-authz.py (review findings MIG-1, MIG-2): the cutover, on a real PostgreSQL.

The script is run once when the new release goes live: it adds an assignment for every membership
that has none (what the previous release wrote after migration 0009) and turns ``auto_enrol`` off. It
is a dry run unless ``--apply`` is given, and never duplicates.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)"
)

SCRIPT = Path(__file__).resolve().parents[2] / "scripts-cutover-authz.py"


@pytest.fixture(scope="module")
def cutover():
    spec = importlib.util.spec_from_file_location("scripts_cutover_authz", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # a dataclass in the script looks its module up here
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(fresh_postgres):
    """A migrated database (to head: 0009) with the IAC (auto_enrol on, as 0004 made it), an ET, and three users."""
    engine = fresh_postgres
    ids = {name: str(uuid4()) for name in ("alice", "bob", "carol")}
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jurisdiction (id, code, name, enabled, auto_enrol, settings, created_datetime, updated_datetime)"
                " VALUES (gen_random_uuid(), 'ET', 'Employment Tribunal', true, true, '{}', now(), now())"
            )
        )
        for name, uid in ids.items():
            connection.execute(
                text(
                    'INSERT INTO "user" (id, created_datetime, email, azure_user_id, has_completed_onboarding, role)'
                    " VALUES (:uid, now(), :email, :oid, true, 'Judge')"
                ),
                {"uid": uid, "email": f"{name.capitalize()}@Example.TEST", "oid": f"oid-{name}"},
            )
    engine.ids = ids  # type: ignore[attr-defined]
    return engine


def membership(engine, *, code: str, role: str, user: str | None = None, email: str | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jurisdiction_membership (id, jurisdiction_id, user_id, email, role, created_datetime)"
                " SELECT gen_random_uuid(), j.id, :uid, :email, :role, now() FROM jurisdiction j WHERE j.code = :code"
            ),
            {"uid": engine.ids[user] if user else None, "email": email, "role": role, "code": code},
        )


def assignments(engine) -> list[tuple]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT coalesce(lower(u.email), a.pending_email), j.code, a.role, a.user_id IS NULL"
                ' FROM role_assignment a LEFT JOIN "user" u ON u.id = a.user_id'
                " JOIN jurisdiction j ON j.id = a.jurisdiction_id ORDER BY 1, 2, 3"
            )
        ).all()
    return [tuple(r) for r in rows]


def auto_enrol(engine) -> list[bool]:
    with engine.connect() as connection:
        return [r[0] for r in connection.execute(text("SELECT auto_enrol FROM jurisdiction ORDER BY code")).all()]


def window(engine) -> None:
    """What the previous release wrote after 0009 ran (so what 0009's one-shot copy missed)."""
    membership(engine, code="ET", role="judge", user="alice")  # an administrator added a judge
    membership(engine, code="ET", role="admin", user="bob")  # ... and an administrator
    membership(engine, code="IAC", role="judge", email="New.Judge@Example.TEST")  # added by email, mixed case
    membership(engine, code="IAC", role="judge", email="  new.judge@example.test")  # the same, padded: one assignment
    membership(engine, code="ET", role="admin", email="")  # no email, no user: nothing to give


def test_the_default_is_a_dry_run_that_prints_the_counts_and_writes_nothing(db, cutover, capsys):
    window(db)
    before = (assignments(db), auto_enrol(db))

    assert cutover.main([], engine=db) == 0

    assert (assignments(db), auto_enrol(db)) == before  # nothing written, auto_enrol included
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "would add" in out
    assert "for users:                     2" in out and "pending by email:              1" in out
    assert "auto_enrol turned off (would be):     2" in out
    assert "***@" in out and ":transcribe@" not in out  # the password is not printed


def test_apply_adds_what_the_old_code_wrote_with_the_migrations_mapping_and_turns_auto_enrol_off(db, cutover, capsys):
    window(db)

    assert cutover.main(["--apply"], engine=db) == 0

    assert assignments(db) == [
        ("alice@example.test", "ET", "Judge", False),
        ("bob@example.test", "ET", "JurisdictionAdministrator", False),
        ("new.judge@example.test", "IAC", "Judge", True),  # pending, lower case, trimmed, once
    ]
    assert auto_enrol(db) == [False, False]
    out = capsys.readouterr().out
    assert "APPLY" in out and "memberships still without an assignment:            0" in out
    assert "Do not run this again" in out


def test_running_it_again_changes_nothing(db, cutover, capsys):
    window(db)
    assert cutover.main(["--apply"], engine=db) == 0
    after_first = assignments(db)
    capsys.readouterr()

    assert cutover.main(["--apply"], engine=db) == 0

    assert assignments(db) == after_first  # no duplicates
    out = capsys.readouterr().out
    assert "for users:                     0" in out and "pending by email:              0" in out
    assert "auto_enrol turned off:     0" in out


def test_what_the_migration_already_copied_is_not_copied_again_whatever_the_case_of_the_email(db, cutover):
    """0009's copy (lower case, not trimmed) and the script's (lower case, trimmed) must agree: no duplicate."""
    membership(db, code="ET", role="judge", user="carol")
    membership(db, code="IAC", role="admin", email="Mixed.Case@Example.TEST")
    with db.begin() as connection:  # what 0009 would have made of them
        connection.execute(
            text(
                "INSERT INTO role_assignment (id, user_id, pending_email, jurisdiction_id, role, granted_at)"
                " SELECT gen_random_uuid(), m.user_id, CASE WHEN m.user_id IS NULL THEN lower(m.email) END,"
                " m.jurisdiction_id, CASE m.role WHEN 'admin' THEN 'JurisdictionAdministrator' ELSE 'Judge' END,"
                " m.created_datetime FROM jurisdiction_membership m"
            )
        )
    before = assignments(db)
    assert len(before) == 2

    assert cutover.main(["--apply"], engine=db) == 0

    assert assignments(db) == before


def test_a_pending_membership_the_old_code_claimed_becomes_the_users_assignment_and_the_pending_copy_goes(db, cutover):
    """0009 copied a pending membership; the previous release then claimed it for the user on their first
    sign-in (the membership has a user now), so the new code would never claim the pending assignment."""
    membership(db, code="IAC", role="judge", email="alice@example.test")
    with db.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO role_assignment (id, pending_email, jurisdiction_id, role, granted_at)"
                " SELECT gen_random_uuid(), 'alice@example.test', m.jurisdiction_id, 'Judge', now()"
                " FROM jurisdiction_membership m"
            )
        )
        connection.execute(
            text("UPDATE jurisdiction_membership SET user_id = :uid, email = NULL"), {"uid": db.ids["alice"]}
        )
    assert assignments(db) == [("alice@example.test", "IAC", "Judge", True)]

    assert cutover.main(["--apply"], engine=db) == 0

    assert assignments(db) == [("alice@example.test", "IAC", "Judge", False)]  # a user's, once; no pending row left


def test_a_pending_assignment_with_no_matching_membership_is_left_alone(db, cutover):
    """Made in the new screens (or whose membership the old admin removed): the script never removes access."""
    with db.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO role_assignment (id, pending_email, jurisdiction_id, role, granted_at)"
                " SELECT gen_random_uuid(), 'alice@example.test', id, 'LegalTextManager', now()"
                " FROM jurisdiction WHERE code = 'ET'"
            )
        )
    before = assignments(db)
    assert cutover.main(["--apply"], engine=db) == 0
    assert assignments(db) == before


def test_it_refuses_a_database_that_has_not_been_migrated_and_writes_nothing(fresh_postgres, cutover, capsys):
    with fresh_postgres.begin() as connection:
        connection.execute(text("DROP TABLE role_assignment CASCADE"))
    assert cutover.main(["--apply"], engine=fresh_postgres) == 1
    assert "run the migrations" in capsys.readouterr().out


def test_without_a_database_url_it_says_so(cutover, monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_CONNECTION_STRING", raising=False)
    assert cutover.main([]) == 2
    assert "DATABASE_CONNECTION_STRING" in capsys.readouterr().out
