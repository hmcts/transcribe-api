"""Migration 0019 (DIAAT-428): the ``hearing`` table, and the hearing and hearing date of each job.

Runs the migration's own upgrade() and downgrade() through Alembic's operations on SQLite, so it is
part of every run (the Postgres checks, including the date in London of a job uploaded near midnight,
are in tests/unit/test_migrations.py, which needs TEST_POSTGRES_URL): the table and its columns, the
two nullable job columns and their index, that every job becomes a one-day hearing with the job's own
id, dated the day it was uploaded in London, that the downgrade removes exactly what was added, and
that the models declare what the migration creates.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from transcribe_api.domain.models_recording import Hearing, SpeechBatchJob

MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0019_hearings.py"


def _load():
    spec = importlib.util.spec_from_file_location("migration_0019", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def connection():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE "user" (id CHAR(32) PRIMARY KEY)'))
        connection.execute(text("CREATE TABLE jurisdiction (id CHAR(32) PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE transcription_job (id CHAR(32) PRIMARY KEY, created_datetime DATETIME NOT NULL,"
                " updated_datetime DATETIME, user_id CHAR(32), jurisdiction_id CHAR(32), status TEXT, metadata JSON)"
            )
        )
        for row in (
            # 23:30 UTC on a summer evening is the next morning in London (BST, UTC+1).
            (
                "a",
                "2026-07-01 23:30:00",
                "u1",
                "j1",
                {"case_reference": "PA/1", "tribunal": "Taylor House", "audio_file_name": "a.mp3"},
            ),
            # In winter London is UTC: the same day.
            ("b", "2026-01-01 23:30:00", "u2", "j1", {}),
        ):
            connection.execute(
                text(
                    "INSERT INTO transcription_job (id, created_datetime, user_id, jurisdiction_id, status, metadata)"
                    " VALUES (:id, :created, :user, :jurisdiction, 'succeeded', :metadata)"
                ),
                {
                    "id": row[0],
                    "created": row[1],
                    "user": row[2],
                    "jurisdiction": row[3],
                    "metadata": json.dumps(row[4]),
                },
            )
        yield connection
    engine.dispose()


def _run(connection, step: str) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_load(), step)()


def test_it_follows_0018():
    module = _load()
    assert (module.revision, module.down_revision) == ("0019", "0018")


def test_upgrade_creates_the_hearing_table(connection):
    _run(connection, "upgrade")

    columns = {c["name"]: c for c in inspect(connection).get_columns("hearing")}
    assert set(columns) == {"id", "created_datetime", "updated_datetime", "user_id", "jurisdiction_id", "metadata"}
    assert {name for name, column in columns.items() if not column["nullable"]} == {"id", "created_datetime"}
    assert sorted(i["column_names"] for i in inspect(connection).get_indexes("hearing")) == [
        ["jurisdiction_id"],
        ["user_id"],
    ]
    assert sorted(f["referred_table"] for f in inspect(connection).get_foreign_keys("hearing")) == [
        "jurisdiction",
        "user",
    ]


def test_upgrade_gives_each_job_a_nullable_hearing_and_date_ordered_by_an_index(connection):
    _run(connection, "upgrade")

    columns = {c["name"]: c for c in inspect(connection).get_columns("transcription_job")}
    assert columns["hearing_id"]["nullable"] is True
    assert columns["hearing_date"]["nullable"] is True
    indexes = {i["name"]: i["column_names"] for i in inspect(connection).get_indexes("transcription_job")}
    assert indexes["ix_transcription_job_hearing_order"] == ["hearing_id", "hearing_date", "created_datetime"]
    (foreign_key,) = [
        f for f in inspect(connection).get_foreign_keys("transcription_job") if f["referred_table"] == "hearing"
    ]
    assert foreign_key["constrained_columns"] == ["hearing_id"]
    assert foreign_key["options"].get("ondelete") == "RESTRICT"


def test_every_job_becomes_a_one_day_hearing_with_its_own_id_dated_in_london(connection):
    _run(connection, "upgrade")

    jobs = connection.execute(text("SELECT id, hearing_id, hearing_date FROM transcription_job ORDER BY id")).all()
    assert [(j[0], j[1], str(j[2])) for j in jobs] == [
        ("a", "a", str(date(2026, 7, 2))),
        ("b", "b", str(date(2026, 1, 1))),
    ]
    hearings = connection.execute(
        text("SELECT id, created_datetime, user_id, jurisdiction_id, metadata FROM hearing ORDER BY id")
    ).all()
    assert [(h[0], str(h[1]), h[2], h[3], json.loads(h[4])) for h in hearings] == [
        ("a", "2026-07-01 23:30:00", "u1", "j1", {"case_reference": "PA/1", "tribunal": "Taylor House"}),
        ("b", "2026-01-01 23:30:00", "u2", "j1", {}),
    ]


def test_downgrade_removes_what_was_added_and_keeps_the_jobs(connection):
    _run(connection, "upgrade")

    _run(connection, "downgrade")

    assert "hearing" not in inspect(connection).get_table_names()
    columns = {c["name"] for c in inspect(connection).get_columns("transcription_job")}
    assert columns == {"id", "created_datetime", "updated_datetime", "user_id", "jurisdiction_id", "status", "metadata"}
    assert connection.execute(text("SELECT id, status FROM transcription_job ORDER BY id")).all() == [
        ("a", "succeeded"),
        ("b", "succeeded"),
    ]


def test_it_can_be_applied_again_after_a_downgrade(connection):
    _run(connection, "upgrade")
    _run(connection, "downgrade")
    _run(connection, "upgrade")
    assert connection.execute(text("SELECT count(*) FROM hearing")).scalar_one() == 2


def test_the_models_declare_what_the_migration_creates():
    table = Hearing.__table__
    assert table.name == "hearing"
    assert {c.name for c in table.columns} == {
        "id",
        "created_datetime",
        "updated_datetime",
        "user_id",
        "jurisdiction_id",
        "metadata",
    }
    assert {c.name for c in table.columns if not c.nullable} == {"id", "created_datetime"}

    jobs = SpeechBatchJob.__table__
    assert jobs.c.hearing_id.nullable and jobs.c.hearing_date.nullable
    (foreign_key,) = jobs.c.hearing_id.foreign_keys
    assert foreign_key.target_fullname == "hearing.id" and foreign_key.ondelete == "RESTRICT"
    (order,) = [i for i in jobs.indexes if i.name == "ix_transcription_job_hearing_order"]
    assert [c.name for c in order.columns] == ["hearing_id", "hearing_date", "created_datetime"]
