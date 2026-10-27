"""Database migrations: one Alembic history, applied at API startup.

The offline tests render the migration SQL without a database. The integration
test needs a real Postgres (TEST_POSTGRES_URL) and is skipped without one; run
it locally against a throwaway container before changing the baseline.
"""

from __future__ import annotations

import io
import os
import re

import pytest
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient

from transcribe_api.runtime import migrations

POSTGRES_URL = "postgresql://user@localhost/db"

EXPECTED_TABLES = {
    "audit_log",
    "caller",
    "correction_dataset_entry",
    "document_content",
    "live_transcript_draft",
    "minuteversion",
    "tag",
    "transcription",
    "transcription_job",
    "transcription_tag_association",
    "transcriptionjob",
    "user",
}


# Columns that were `timestamp with time zone` in the old schemas (see
# docs/data-migration/schema-mapping.md). 0001 created them naive; 0002 restores
# them. Every other timestamp column was naive in the old schemas and stays so.
AWARE_TIMESTAMP_COLUMNS = {
    "audit_log": ["timestamp"],
    "caller": ["created_datetime", "updated_datetime"],
    "correction_dataset_entry": ["created_datetime", "updated_datetime"],
    "document_content": ["updated_at"],
    "live_transcript_draft": ["created_datetime", "updated_datetime"],
    "transcription_job": ["created_datetime", "updated_datetime", "webhook_dispatched_at"],
}

# Timestamp columns added after 0002, created timestamptz from the start.
ADDED_AWARE_TIMESTAMP_COLUMNS = {
    "transcription_job": ["processing_started_datetime", "stuck_reported_at"],  # 0003
}

# Dictation migration a8f2c9d5e1b3 (fix for 15-20s p95 latency); 0001 lost them.
FK_INDEXES = {
    "ix_transcription_user_id": ("transcription", "user_id"),
    "ix_minuteversion_transcription_id": ("minuteversion", "transcription_id"),
    "ix_transcriptionjob_transcription_id": ("transcriptionjob", "transcription_id"),
}


def _offline_sql(monkeypatch, revision: str = "head", downgrade: bool = False) -> str:
    monkeypatch.setenv("DATABASE_CONNECTION_STRING", POSTGRES_URL)
    buffer = io.StringIO()
    config = migrations.alembic_config()
    config.output_buffer = buffer
    from alembic import command

    if downgrade:
        command.downgrade(config, revision, sql=True)
    else:
        command.upgrade(config, revision, sql=True)
    return buffer.getvalue()


def _alter_type_sql(table: str, column: str, type_sql: str) -> re.Pattern[str]:
    return re.compile(
        rf'ALTER TABLE "?{table}"? ALTER COLUMN "?{column}"? TYPE {type_sql} '
        rf"""USING "?{column}"? AT TIME ZONE 'UTC'"""
    )


def test_there_is_one_history_with_one_head():
    script = ScriptDirectory.from_config(migrations.alembic_config())
    assert script.get_bases() == ["0001"]
    assert script.get_heads() == ["0003"]


def test_baseline_creates_every_table(monkeypatch):
    sql = _offline_sql(monkeypatch)
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE {table} " in sql or f'CREATE TABLE "{table}" ' in sql, table


def test_baseline_keeps_the_schema_rules_that_only_old_migrations_had(monkeypatch):
    sql = _offline_sql(monkeypatch)
    # dictation 8e2a5d9f3b7c: user.role limited to the app roles
    assert "CONSTRAINT ck_user_role_valid CHECK" in sql
    # recording 007: GIN index on dialogue_entries
    assert "ix_transcription_job_dialogue_entries_gin" in sql
    assert "USING gin (dialogue_entries)" in sql
    # recording 012: idempotency for JWT callers
    assert "CREATE UNIQUE INDEX ix_transcription_job_user_idempotency" in sql
    assert "WHERE user_id IS NOT NULL AND idempotency_key IS NOT NULL" in sql


def test_0002_restores_the_dictation_foreign_key_indexes(monkeypatch):
    sql = _offline_sql(monkeypatch, "0001:0002")
    for name, (table, column) in FK_INDEXES.items():
        assert f"CREATE INDEX {name} ON {table} ({column})" in sql, name


def test_0002_restores_timezones_reading_existing_values_as_utc(monkeypatch):
    sql = _offline_sql(monkeypatch, "0001:0002")
    for table, columns in AWARE_TIMESTAMP_COLUMNS.items():
        for column in columns:
            assert _alter_type_sql(table, column, "TIMESTAMP WITH TIME ZONE").search(sql), f"{table}.{column}"
    # Nothing else changes type: the columns that were naive stay naive.
    altered = len(re.findall(r"ALTER COLUMN \S+ TYPE", sql))
    assert altered == sum(len(c) for c in AWARE_TIMESTAMP_COLUMNS.values())


def test_0002_downgrade_reverses_it(monkeypatch):
    sql = _offline_sql(monkeypatch, "0002:0001", downgrade=True)
    for table, columns in AWARE_TIMESTAMP_COLUMNS.items():
        for column in columns:
            assert _alter_type_sql(table, column, "TIMESTAMP WITHOUT TIME ZONE").search(sql), f"{table}.{column}"
    for name, (table, _column) in FK_INDEXES.items():
        assert f"DROP INDEX {name}" in sql, name


def test_the_models_declare_the_restored_timezones_and_indexes():
    """Without a database: the models must say what 0002 does, or autogenerate drifts."""
    from sqlalchemy import DateTime

    tables = migrations.target_metadata().tables
    for table in tables.values():
        for column in table.columns:
            if isinstance(column.type, DateTime):
                expected = column.name in (
                    AWARE_TIMESTAMP_COLUMNS.get(table.name, []) + ADDED_AWARE_TIMESTAMP_COLUMNS.get(table.name, [])
                )
                assert column.type.timezone is expected, f"{table.name}.{column.name}"
    for name, (table, column) in FK_INDEXES.items():
        index = next((i for i in tables[table].indexes if i.name == name), None)
        assert index is not None, name
        assert [c.name for c in index.columns] == [column]
        assert not index.unique


# 0003: DIAAT-205 observability columns -------------------------------------------------

OBSERVABILITY_COLUMNS = {
    "audit_log": {
        "outcome": "VARCHAR(16)",
        "object_type": "VARCHAR(32)",
        "object_id": "VARCHAR(64)",
        "request_id": "VARCHAR(64)",
    },
    "transcription_job": {
        "trace_parent": "VARCHAR(64)",
        "processing_started_datetime": "TIMESTAMP WITH TIME ZONE",
        "stuck_reported_at": "TIMESTAMP WITH TIME ZONE",
        "provenance": "JSONB",
    },
    "transcriptionjob": {"provenance": "JSONB"},
}


def test_0003_adds_the_observability_columns_nullable(monkeypatch):
    sql = _offline_sql(monkeypatch, "0002:0003")
    for table, columns in OBSERVABILITY_COLUMNS.items():
        for column, type_sql in columns.items():
            # Nullable with no default: existing rows are untouched.
            assert f"ALTER TABLE {table} ADD COLUMN {column} {type_sql};" in sql, f"{table}.{column}"
    assert "CREATE INDEX ix_audit_log_object_id ON audit_log (object_id)" in sql
    assert "CREATE INDEX ix_audit_log_request_id ON audit_log (request_id)" in sql
    assert "ALTER TABLE audit_log ALTER COLUMN email DROP NOT NULL" in sql
    # The recording service's own audit table is not created: it writes audit_log.
    assert "CREATE TABLE" not in sql


def test_0003_downgrade_reverses_it(monkeypatch):
    sql = _offline_sql(monkeypatch, "0003:0002", downgrade=True)
    for table, columns in OBSERVABILITY_COLUMNS.items():
        for column in columns:
            assert f"ALTER TABLE {table} DROP COLUMN {column};" in sql, f"{table}.{column}"
    # Rows written without an email get an empty one before NOT NULL comes back.
    assert sql.index("UPDATE audit_log SET email = '' WHERE email IS NULL") < sql.index(
        "ALTER TABLE audit_log ALTER COLUMN email SET NOT NULL"
    )


def test_the_models_declare_the_observability_columns():
    tables = migrations.target_metadata().tables
    for table, columns in OBSERVABILITY_COLUMNS.items():
        for column in columns:
            assert tables[table].columns[column].nullable, f"{table}.{column}"
    assert tables["audit_log"].columns["email"].nullable
    indexed = {next(iter(i.columns)).name for i in tables["audit_log"].indexes}
    assert {"object_id", "request_id"} <= indexed


class _FakeConnection:
    def __init__(self, log: list[str]):
        self.log = log

    def execute(self, statement, parameters=None):
        self.log.append(str(statement))

    def commit(self):
        self.log.append("COMMIT")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeEngine:
    def __init__(self, log: list[str]):
        self.connection = _FakeConnection(log)

    def connect(self):
        return self.connection


def test_upgrade_runs_under_an_advisory_lock(monkeypatch):
    """Several API pods start at once; only one may migrate at a time."""
    log: list[str] = []
    engine = _FakeEngine(log)
    seen = {}

    def fake_upgrade(config, revision):
        seen["connection"] = config.attributes.get("connection")
        log.append(f"upgrade {revision}")

    monkeypatch.setattr(migrations.command, "upgrade", fake_upgrade)

    migrations.run_migrations(engine)

    lock = next(i for i, s in enumerate(log) if "pg_advisory_lock" in s)
    upgrade = log.index("upgrade head")
    unlock = next(i for i, s in enumerate(log) if "pg_advisory_unlock" in s)
    assert lock < upgrade < unlock
    assert seen["connection"] is engine.connection


def test_lock_is_released_when_the_upgrade_fails(monkeypatch):
    log: list[str] = []

    def failing_upgrade(config, revision):
        raise RuntimeError("bad migration")

    monkeypatch.setattr(migrations.command, "upgrade", failing_upgrade)

    with pytest.raises(RuntimeError, match="bad migration"):
        migrations.run_migrations(_FakeEngine(log))
    assert any("pg_advisory_unlock" in s for s in log)


def _startup_with(monkeypatch, run_migrations_env: str | None, recorder):
    if run_migrations_env is None:
        monkeypatch.delenv("RUN_MIGRATIONS", raising=False)
    else:
        monkeypatch.setenv("RUN_MIGRATIONS", run_migrations_env)
    monkeypatch.setattr(migrations, "run_migrations", recorder)
    from transcribe_api.api.app import create_app

    with TestClient(create_app()):
        pass


def test_startup_migrates_when_enabled(monkeypatch):
    calls = []
    _startup_with(monkeypatch, "true", lambda: calls.append(1))
    assert calls == [1]


def test_startup_does_not_migrate_by_default(monkeypatch):
    calls = []
    _startup_with(monkeypatch, None, lambda: calls.append(1))
    assert calls == []


def test_a_failed_migration_stops_startup(monkeypatch):
    def boom():
        raise RuntimeError("migration failed")

    with pytest.raises(RuntimeError, match="migration failed"):
        _startup_with(monkeypatch, "true", boom)


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)")
def test_baseline_matches_the_models_on_a_real_database():
    """Upgrade an empty database, then compare it with the models: no drift."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine, text

    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))

    migrations.run_migrations(engine)

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, migrations.target_metadata())
    assert diff == []


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)")
def test_0002_keeps_existing_naive_values_as_utc_whatever_the_session_timezone():
    """Rows written under 0001 hold UTC wall-clock time; 0002 must not shift them."""
    from datetime import UTC, datetime

    from alembic import command
    from sqlalchemy import create_engine, text

    engine = create_engine(os.environ["TEST_POSTGRES_URL"], connect_args={"options": "-c timezone=Europe/London"})
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))

    def migrate(step, revision):
        with engine.begin() as connection:
            config = migrations.alembic_config()
            config.attributes["connection"] = connection
            step(config, revision)

    migrate(command.upgrade, "0001")
    summer_noon_utc = datetime(2026, 7, 1, 12, 0)  # BST, so London is UTC+1
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_log (id, timestamp, event_type, user_id, email, held_roles, required_roles)"
                " VALUES (gen_random_uuid(), :ts, 'denied', 'u', 'e', '[]', '[]')"
            ),
            {"ts": summer_noon_utc},
        )

    migrate(command.upgrade, "head")
    with engine.connect() as connection:
        stored = connection.execute(text("SELECT timestamp FROM audit_log")).scalar_one()
    assert stored == summer_noon_utc.replace(tzinfo=UTC)

    migrate(command.downgrade, "0001")
    with engine.connect() as connection:
        stored = connection.execute(text("SELECT timestamp FROM audit_log")).scalar_one()
        indexes = (
            connection.execute(
                text("SELECT indexname FROM pg_indexes WHERE indexname = ANY(:names)"),
                {"names": list(FK_INDEXES)},
            )
            .scalars()
            .all()
        )
    assert stored == summer_noon_utc
    assert indexes == []


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)")
def test_0003_keeps_existing_rows_and_allows_app_rows_without_an_email():
    from alembic import command
    from sqlalchemy import create_engine, text

    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))

    def migrate(step, revision):
        with engine.begin() as connection:
            config = migrations.alembic_config()
            config.attributes["connection"] = connection
            step(config, revision)

    migrate(command.upgrade, "0002")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_log (id, timestamp, event_type, user_id, email, held_roles, required_roles)"
                " VALUES (gen_random_uuid(), now(), 'ACCESS_DENIED', 'oid', 'judge@example.test', '[]', '[]')"
            )
        )

    migrate(command.upgrade, "0003")
    with engine.begin() as connection:
        old = connection.execute(text("SELECT email, outcome, object_id FROM audit_log")).one()
        assert tuple(old) == ("judge@example.test", None, None)
        connection.execute(
            text(
                "INSERT INTO audit_log (id, timestamp, event_type, user_id, held_roles, required_roles,"
                " outcome, object_type, object_id, request_id)"
                " VALUES (gen_random_uuid(), now(), 'transcript.view', :uid, '[]', '[]',"
                " 'success', 'transcription', :oid, :rid)"
            ),
            {
                "uid": "3f2a9c1e-8b7d-4e6f-9a1b-2c3d4e5f6a7b",
                "oid": "0f8fad5b-d9cb-469f-a165-70867728950e",
                "rid": "0f8fad5bd9cb469fa16570867728950e",
            },
        )

    migrate(command.downgrade, "0002")
    with engine.connect() as connection:
        emails = sorted(connection.execute(text("SELECT email FROM audit_log")).scalars().all())
    assert emails == ["", "judge@example.test"]
