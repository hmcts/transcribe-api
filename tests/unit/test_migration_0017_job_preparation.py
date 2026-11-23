"""Migration 0017 (DIAAT-408): ``transcription_job.source_blob_path`` and ``preparation_attempts``.

Runs the migration's own upgrade() and downgrade() through Alembic's operations on SQLite, so it is
part of every run (the Postgres checks are in tests/unit/test_migrations.py, which needs
TEST_POSTGRES_URL): the columns, their nullability and default, that rows from before survive both
ways, that the downgrade drops exactly these two, and that the model declares what the migration adds.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from transcribe_api.domain.models_recording import SpeechBatchJob

MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0017_job_preparation.py"


def _load():
    spec = importlib.util.spec_from_file_location("migration_0017", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def connection():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE transcription_job (id TEXT PRIMARY KEY, status TEXT, audio_url TEXT)"))
        connection.execute(text("INSERT INTO transcription_job VALUES ('a', 'PENDING', 'https://x/a.wav')"))
        yield connection
    engine.dispose()


def _run(connection, step: str) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_load(), step)()


def _columns(connection) -> dict[str, dict]:
    return {column["name"]: column for column in inspect(connection).get_columns("transcription_job")}


def test_it_follows_0016():
    module = _load()
    assert (module.revision, module.down_revision) == ("0017", "0016")


def test_upgrade_adds_a_nullable_path_and_a_non_null_attempt_count_defaulting_to_zero(connection):
    _run(connection, "upgrade")

    columns = _columns(connection)
    assert columns["source_blob_path"]["nullable"] is True
    assert columns["preparation_attempts"]["nullable"] is False
    assert str(columns["preparation_attempts"]["default"]).strip("'\"") == "0"
    # The row from before is intact: no path, no attempts.
    assert connection.execute(
        text("SELECT id, status, source_blob_path, preparation_attempts FROM transcription_job")
    ).all() == [("a", "PENDING", None, 0)]


def test_a_row_written_by_the_previous_release_is_valid(connection):
    """The previous release's pods do not know the columns: their inserts still work."""
    _run(connection, "upgrade")
    connection.execute(
        text("INSERT INTO transcription_job (id, status, audio_url) VALUES ('b', 'PENDING', 'https://x/b.wav')")
    )
    assert connection.execute(text("SELECT preparation_attempts FROM transcription_job WHERE id = 'b'")).scalar() == 0


def test_downgrade_drops_exactly_the_two_columns_and_keeps_every_row(connection):
    _run(connection, "upgrade")
    connection.execute(
        text("UPDATE transcription_job SET source_blob_path = 'incoming/u/x.mp3', preparation_attempts = 2")
    )

    _run(connection, "downgrade")

    assert set(_columns(connection)) == {"id", "status", "audio_url"}
    assert connection.execute(text("SELECT id, status, audio_url FROM transcription_job")).all() == [
        ("a", "PENDING", "https://x/a.wav")
    ]


def test_it_can_be_applied_again_after_a_downgrade(connection):
    _run(connection, "upgrade")
    _run(connection, "downgrade")
    _run(connection, "upgrade")
    assert {"source_blob_path", "preparation_attempts"} <= set(_columns(connection))


def test_the_model_declares_what_the_migration_adds():
    table = SpeechBatchJob.__table__
    assert table.c.source_blob_path.nullable is True
    assert str(table.c.source_blob_path.type) == "VARCHAR"
    assert table.c.preparation_attempts.nullable is False
    assert str(table.c.preparation_attempts.type) == "INTEGER"
    assert table.c.preparation_attempts.server_default.arg == "0"
