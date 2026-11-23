"""Migration 0018 (DIAAT-408): the ``transcription_job_part`` table.

Runs the migration's own upgrade() and downgrade() through Alembic's operations on SQLite, so it is
part of every run (the Postgres checks are in tests/unit/test_migrations.py, which needs
TEST_POSTGRES_URL): the table and its columns, the unique part index per job, the index on job_id,
that the job rows survive both ways, that the downgrade removes exactly this table, and that the
model declares what the migration creates.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from transcribe_api.domain.models_recording import TranscriptionJobPart

MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0018_job_parts.py"


def _load():
    spec = importlib.util.spec_from_file_location("migration_0018", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def connection():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE transcription_job (id CHAR(32) PRIMARY KEY, status TEXT)"))
        connection.execute(text("INSERT INTO transcription_job VALUES ('a', 'PENDING')"))
        yield connection
    engine.dispose()


def _run(connection, step: str) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_load(), step)()


def _insert(connection, **values) -> None:
    row = {
        "id": "p1",
        "created_datetime": "2026-11-20 10:00:00",
        "job_id": "a",
        "part_index": 1,
        "part_count": 3,
        "offset_seconds": 0.0,
        "duration_seconds": 9600.0,
        "cut_kind": "start",
        "status": "pending",
        "attempts": 0,
        **values,
    }
    connection.execute(
        text(f"INSERT INTO transcription_job_part ({', '.join(row)}) VALUES ({', '.join(':' + k for k in row)})"), row
    )


def test_it_follows_0017():
    module = _load()
    assert (module.revision, module.down_revision) == ("0018", "0017")


def test_upgrade_creates_the_table_with_the_columns_the_parts_need(connection):
    _run(connection, "upgrade")

    columns = {c["name"]: c for c in inspect(connection).get_columns("transcription_job_part")}
    assert set(columns) == {
        "id", "created_datetime", "updated_datetime", "job_id", "part_index", "part_count", "offset_seconds",
        "duration_seconds", "cut_kind", "blob_path", "status", "attempts", "batch_job_id", "batch_job_url",
        "batch_job_status", "error_message",
    }  # fmt: skip
    required = {
        "id",
        "created_datetime",
        "job_id",
        "part_index",
        "part_count",
        "offset_seconds",
        "duration_seconds",
        "cut_kind",
        "status",
        "attempts",
    }
    assert {name for name, column in columns.items() if not column["nullable"]} == required
    assert [i["column_names"] for i in inspect(connection).get_indexes("transcription_job_part")] == [["job_id"]]
    assert [
        (f["referred_table"], f["options"].get("ondelete"))
        for f in inspect(connection).get_foreign_keys("transcription_job_part")
    ] == [("transcription_job", "CASCADE")]


def test_a_job_has_one_row_per_part_index(connection):
    _run(connection, "upgrade")
    _insert(connection)
    _insert(connection, id="p2", part_index=2, offset_seconds=9600.0)
    with pytest.raises(IntegrityError):
        _insert(connection, id="p3", part_index=2)


def test_downgrade_removes_the_table_and_nothing_else(connection):
    _run(connection, "upgrade")
    _insert(connection)

    _run(connection, "downgrade")

    assert "transcription_job_part" not in inspect(connection).get_table_names()
    assert connection.execute(text("SELECT id, status FROM transcription_job")).all() == [("a", "PENDING")]


def test_it_can_be_applied_again_after_a_downgrade(connection):
    _run(connection, "upgrade")
    _run(connection, "downgrade")
    _run(connection, "upgrade")
    assert "transcription_job_part" in inspect(connection).get_table_names()


def test_the_model_declares_what_the_migration_creates():
    table = TranscriptionJobPart.__table__
    assert table.name == "transcription_job_part"
    assert {c.name for c in table.columns if not c.nullable} == {
        "id", "created_datetime", "job_id", "part_index", "part_count", "offset_seconds", "duration_seconds",
        "cut_kind", "status", "attempts",
    }  # fmt: skip
    (foreign_key,) = table.c.job_id.foreign_keys
    assert foreign_key.target_fullname == "transcription_job.id" and foreign_key.ondelete == "CASCADE"
    assert any(c.name == "uq_transcription_job_part_index" for c in table.constraints)
    assert [i.name for i in table.indexes] == ["ix_transcription_job_part_job_id"]
