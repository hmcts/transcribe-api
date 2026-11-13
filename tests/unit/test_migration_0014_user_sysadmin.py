"""Migration 0014 (DIAAT-407): ``user.sysadmin``, seeded from the retired ``user.role``, on a real PostgreSQL."""

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


def add_user(engine, name: str, role: str | None) -> None:
    run(
        engine,
        'INSERT INTO "user" (id, created_datetime, email, azure_user_id, has_completed_onboarding, role)'
        " VALUES (:uid, now(), :email, :oid, true, :role)",
        uid=str(uuid4()),
        email=f"{name}@example.test",
        oid=f"oid-{name}",
        role=role,
    )


def test_0014_marks_the_users_whose_retired_role_was_system_administrator(engine):
    migrate(engine, "0013")
    add_user(engine, "ada", "SystemAdministrator")
    add_user(engine, "bea", "Judge")
    add_user(engine, "cal", None)

    migrate(engine, "0014")

    assert sorted(rows(engine, 'SELECT email, sysadmin FROM "user"')) == [
        ("ada@example.test", True),
        ("bea@example.test", False),
        ("cal@example.test", False),
    ]
    add_user(engine, "dan", None)
    assert rows(engine, "SELECT sysadmin FROM \"user\" WHERE email = 'dan@example.test'") == [(False,)]


def test_0014_downgrade_drops_the_column(engine):
    migrate(engine, "0014")
    migrate(engine, "0013", down=True)
    assert (
        rows(
            engine,
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'user' AND column_name = 'sysadmin'",
        )
        == []
    )
