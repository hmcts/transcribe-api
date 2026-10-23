"""Database migrations: one Alembic history, applied at API startup.

The offline tests render the migration SQL without a database. The integration
test needs a real Postgres (TEST_POSTGRES_URL) and is skipped without one; run
it locally against a throwaway container before changing the baseline.
"""

from __future__ import annotations

import io
import os

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


def _offline_sql(monkeypatch) -> str:
    monkeypatch.setenv("DATABASE_CONNECTION_STRING", POSTGRES_URL)
    buffer = io.StringIO()
    config = migrations.alembic_config()
    config.output_buffer = buffer
    from alembic import command

    command.upgrade(config, "head", sql=True)
    return buffer.getvalue()


def test_there_is_one_history_with_one_head():
    script = ScriptDirectory.from_config(migrations.alembic_config())
    assert script.get_bases() == ["0001"]
    assert script.get_heads() == ["0001"]


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
