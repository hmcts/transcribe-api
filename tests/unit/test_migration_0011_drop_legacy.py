"""Migration 0011 (DIAAT-405): the access data 0010 retired is dropped, on a real PostgreSQL."""

from __future__ import annotations

import os
import threading
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from alembic import command
from transcribe_api.domain.models_dictation import Jurisdiction
from transcribe_api.runtime import migrations

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)"
)

LEGACY = "jurisdiction_membership_legacy"


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


def tables(engine) -> set[str]:
    return {
        row[0] for row in rows(engine, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    }


def columns(engine, table: str) -> list[tuple]:
    return rows(
        engine,
        "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns"
        f" WHERE table_name = '{table}' ORDER BY ordinal_position",
    )


def shape(engine, table: str) -> dict[str, list]:
    """Columns, constraints and indexes, by name and definition."""
    return {
        "columns": columns(engine, table),
        "constraints": rows(
            engine,
            f"SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = '{table}'::regclass ORDER BY 1",
        ),
        "indexes": rows(engine, f"SELECT indexname, indexdef FROM pg_indexes WHERE tablename = '{table}' ORDER BY 1"),
    }


def leftovers(engine) -> list[tuple]:
    """Anything still named after the membership table: its indexes, keys, constraints or a sequence."""
    return rows(
        engine, "SELECT relname, relkind::text FROM pg_class WHERE relname LIKE '%jurisdiction_membership%'"
    ) + rows(engine, "SELECT conname, contype::text FROM pg_constraint WHERE conname LIKE '%jurisdiction_membership%'")


def everything_else(engine) -> dict[str, list]:
    return {
        "users": rows(engine, 'SELECT id, email, role FROM "user" ORDER BY id'),
        "jurisdictions": rows(
            engine, "SELECT id, code, name, enabled, settings::text, stage FROM jurisdiction ORDER BY code"
        ),
        "assignments": rows(
            engine,
            "SELECT user_id, pending_email, jurisdiction_id, role, tester FROM role_assignment ORDER BY 1, 2, 3, 4",
        ),
    }


@pytest.fixture
def at_0010(engine):
    """Memberships before 0009, carried over by 0009 and 0010: the legacy table holds them at 0010."""
    migrate(engine, "0008")
    run(
        engine,
        "INSERT INTO jurisdiction (id, code, name, enabled, auto_enrol, settings, created_datetime, updated_datetime)"
        " VALUES (gen_random_uuid(), 'ET', 'Employment Tribunal', true, true, '{}', now(), now())",
    )
    for name in ("alice", "bob"):
        run(
            engine,
            'INSERT INTO "user" (id, created_datetime, email, azure_user_id, has_completed_onboarding, role)'
            " VALUES (:uid, now(), :email, :oid, true, 'Judge')",
            uid=str(uuid4()),
            email=f"{name}@example.test",
            oid=f"oid-{name}",
        )
    for code, role, who, tester in (
        ("IAC", "judge", "alice", False),
        ("ET", "admin", "bob", True),
    ):
        run(
            engine,
            "INSERT INTO jurisdiction_membership (id, jurisdiction_id, user_id, role, tester, created_datetime)"
            ' SELECT gen_random_uuid(), j.id, u.id, :role, :tester, now() FROM jurisdiction j, "user" u'
            " WHERE j.code = :code AND u.email = :email",
            code=code,
            role=role,
            tester=tester,
            email=f"{who}@example.test",
        )
    run(
        engine,
        "INSERT INTO jurisdiction_membership (id, jurisdiction_id, email, role, created_datetime)"
        " SELECT gen_random_uuid(), id, 'pending@example.test', 'judge', now() FROM jurisdiction WHERE code = 'IAC'",
    )
    migrate(engine, "0010")
    assert rows(engine, f"SELECT count(*) FROM {LEGACY}") == [(3,)]
    return engine


def test_0011_drops_the_legacy_table_and_auto_enrol_and_keeps_everything_else(at_0010):
    before = everything_else(at_0010)
    assert len(before["assignments"]) == 3

    migrate(at_0010, "0011")

    assert LEGACY not in tables(at_0010)
    assert "auto_enrol" not in [column[0] for column in columns(at_0010, "jurisdiction")]
    assert leftovers(at_0010) == []
    assert everything_else(at_0010) == before
    # Only the columns 0011 has: later revisions' (0012's sign-off) are not there yet.
    with at_0010.begin() as connection:
        connection.execute(
            Jurisdiction.__table__.insert().values(
                id=uuid4(), code="SSCS", name="Social Security and Child Support", enabled=True, settings={}
            )
        )


def test_0011_runs_where_the_table_and_the_column_are_already_gone(at_0010):
    run(at_0010, f"DROP TABLE {LEGACY}")
    run(at_0010, "ALTER TABLE jurisdiction DROP COLUMN auto_enrol")

    migrate(at_0010, "0011")

    assert LEGACY not in tables(at_0010)
    assert "auto_enrol" not in [column[0] for column in columns(at_0010, "jurisdiction")]


def test_0011_runs_where_only_the_column_is_left(at_0010):
    run(at_0010, f"DROP TABLE {LEGACY}")

    migrate(at_0010, "0011")

    assert "auto_enrol" not in [column[0] for column in columns(at_0010, "jurisdiction")]


def test_0011_downgrade_puts_back_the_empty_table_and_the_column_as_0010_left_them(at_0010):
    legacy_at_0010 = shape(at_0010, LEGACY)
    auto_enrol_at_0010 = [column for column in columns(at_0010, "jurisdiction") if column[0] == "auto_enrol"]

    migrate(at_0010, "0011")
    migrate(at_0010, "0010", down=True)

    assert shape(at_0010, LEGACY) == legacy_at_0010
    assert rows(at_0010, f"SELECT count(*) FROM {LEGACY}") == [(0,)]
    assert [column for column in columns(at_0010, "jurisdiction") if column[0] == "auto_enrol"] == auto_enrol_at_0010
    assert rows(at_0010, "SELECT DISTINCT auto_enrol FROM jurisdiction") == [(False,)]

    # The older downgrades find what they drop by name, then the history goes up again.
    migrate(at_0010, "0003", down=True)
    assert leftovers(at_0010) == []
    migrate(at_0010, "head")
    assert LEGACY not in tables(at_0010)


def test_0011_waits_for_a_request_reading_the_users_and_then_drops(at_0010):
    """A request has read a user (every authenticated request does) when 0011 starts. 0011 needs the users
    table exclusively to drop the legacy table's key to it: it gives way, trying again, until the request ends."""
    request = at_0010.connect()
    transaction = request.begin()
    request.execute(text('SELECT id FROM "user" LIMIT 1'))
    errors: list[BaseException] = []

    def upgrade():
        try:
            migrate(at_0010, "0011")
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=upgrade)
    worker.start()
    worker.join(timeout=1.5)
    assert worker.is_alive()
    request.execute(text("SELECT code FROM jurisdiction"))  # the request goes on: 0011 gives way to it
    transaction.commit()
    request.close()
    worker.join(timeout=60)

    assert not worker.is_alive() and errors == []
    assert LEGACY not in tables(at_0010)
