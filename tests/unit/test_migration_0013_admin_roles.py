"""Migration 0013 (DIAAT-406): a JurisdictionAdministrator keeps Judge and LegalTextManager as rows of
its own, on a real PostgreSQL."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from alembic import command
from transcribe_api.runtime import migrations

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)"
)


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


def rows(engine, sql: str) -> list[tuple]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(sql)).all()]


def assignments(engine) -> set[tuple]:
    return set(
        rows(
            engine,
            "SELECT coalesce(u.email, a.pending_email), j.code, a.role FROM role_assignment a"
            ' LEFT JOIN "user" u ON u.id = a.user_id LEFT JOIN jurisdiction j ON j.id = a.jurisdiction_id',
        )
    )


def assign(engine, *, role: str, code: str | None, email: str | None = None, pending: str | None = None) -> None:
    run(
        engine,
        "INSERT INTO role_assignment (id, user_id, pending_email, jurisdiction_id, role, granted_at, tester)"
        ' VALUES (gen_random_uuid(), (SELECT id FROM "user" WHERE email = :email), :pending,'
        " (SELECT id FROM jurisdiction WHERE code = :code), :role, now(), false)",
        email=email,
        pending=pending,
        code=code,
        role=role,
    )


@pytest.fixture
def at_0012(engine):
    migrate(engine, "0012")
    run(
        engine,
        "INSERT INTO jurisdiction (id, code, name, enabled, settings, stage, created_datetime, updated_datetime)"
        " VALUES (gen_random_uuid(), 'ET', 'Employment Tribunal', true, '{}', 'live', now(), now())",
    )
    for name in ("ada", "bea", "cal", "dan"):
        run(
            engine,
            'INSERT INTO "user" (id, created_datetime, email, azure_user_id, has_completed_onboarding)'
            " VALUES (:uid, now(), :email, :oid, true)",
            uid=str(uuid4()),
            email=f"{name}@example.test",
            oid=f"oid-{name}",
        )
    assign(engine, role="JurisdictionAdministrator", code="IAC", email="ada@example.test")
    assign(engine, role="JurisdictionAdministrator", code="ET", email="ada@example.test")
    assign(engine, role="JurisdictionAdministrator", code="IAC", email="bea@example.test")
    assign(engine, role="Judge", code="IAC", email="bea@example.test")
    assign(engine, role="Judge", code="ET", email="cal@example.test")
    assign(engine, role="JurisdictionSuperAdministrator", code=None, email="dan@example.test")
    assign(engine, role="JurisdictionAdministrator", code="ET", pending="new@example.test")
    return engine


def test_0013_gives_each_administrator_judge_and_legal_text_manager_where_it_administers(at_0012):
    before = assignments(at_0012)
    migrate(at_0012, "0013")
    added = assignments(at_0012) - before
    assert added == {
        ("ada@example.test", "IAC", "Judge"),
        ("ada@example.test", "IAC", "LegalTextManager"),
        ("ada@example.test", "ET", "Judge"),
        ("ada@example.test", "ET", "LegalTextManager"),
        ("bea@example.test", "IAC", "LegalTextManager"),
        ("new@example.test", "ET", "Judge"),
        ("new@example.test", "ET", "LegalTextManager"),
    }
    assert before <= assignments(at_0012)


def test_0013_runs_twice_without_adding_anything(at_0012):
    migrate(at_0012, "0013")
    after = rows(at_0012, "SELECT count(*) FROM role_assignment")
    migrate(at_0012, "0012", down=True)
    migrate(at_0012, "0013")
    assert rows(at_0012, "SELECT count(*) FROM role_assignment") == after


def test_0013_downgrade_keeps_the_rows(at_0012):
    migrate(at_0012, "0013")
    after = assignments(at_0012)
    migrate(at_0012, "0012", down=True)
    assert assignments(at_0012) == after
