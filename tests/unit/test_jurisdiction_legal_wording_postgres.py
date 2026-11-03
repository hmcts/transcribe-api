"""DIAAT-342 migration 0006 on a real PostgreSQL (TEST_POSTGRES_URL, a throwaway one): the
existing legal wording becomes the IAC's, id 1 stays the IAC's, one row per jurisdiction,
and the code of the previous release keeps working on the migrated schema.

Skipped without TEST_POSTGRES_URL: run it (see tests/unit/test_migrations.py) before
changing 0006.
"""

from __future__ import annotations

import json
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from transcribe_api.documents import content_loader
from transcribe_api.runtime import migrations

IAC_ID = UUID("6a1c0f3e-3b7d-4c2a-9e51-1ac000000001")

needs_postgres = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)"
)
pytestmark = needs_postgres

@pytest.fixture
def pg(fresh_postgres, monkeypatch):
    """A freshly migrated database, which the content loader and saver use."""
    from transcribe_api.runtime import db_connection

    monkeypatch.setattr(db_connection, "get_engine", lambda: fresh_postgres)
    content_loader.clear_cache()
    yield fresh_postgres
    content_loader.clear_cache()


def _add_jurisdiction(engine, code: str) -> UUID:
    jurisdiction_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jurisdiction (id, code, name, enabled, auto_enrol, settings, created_datetime,"
                " updated_datetime) VALUES (:id, :code, :code, true, false, '{}', now(), now())"
            ),
            {"id": jurisdiction_id, "code": code},
        )
    return jurisdiction_id


def _rows(engine) -> list[tuple]:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT id, jurisdiction_id, content::text FROM document_content ORDER BY id")
        ).all()


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------


def test_the_database_refuses_id_1_for_any_jurisdiction_but_the_iac(pg):
    et = _add_jurisdiction(pg, "ET")
    with pytest.raises(IntegrityError, match="ck_document_content_id_1_is_iac"), pg.begin() as connection:
        connection.execute(
            text("INSERT INTO document_content (id, jurisdiction_id, content, updated_at) VALUES (1, :j, '{}', now())"),
            {"j": et},
        )


def test_the_database_keeps_one_row_per_jurisdiction_and_none_without_one(pg):
    et = _add_jurisdiction(pg, "ET")
    insert = text("INSERT INTO document_content (jurisdiction_id, content, updated_at) VALUES (:j, '{}', now())")
    with pg.begin() as connection:
        connection.execute(insert, {"j": et})
    with pytest.raises(IntegrityError), pg.begin() as connection:
        connection.execute(insert, {"j": et})
    with pytest.raises(IntegrityError), pg.begin() as connection:  # no such jurisdiction
        connection.execute(insert, {"j": uuid4()})
    with pytest.raises(IntegrityError), pg.begin() as connection:  # no jurisdiction named: the IAC, id generated
        connection.execute(text("INSERT INTO document_content (content, updated_at) VALUES ('{}', now())"))
        connection.execute(text("INSERT INTO document_content (content, updated_at) VALUES ('{}', now())"))


def test_a_pod_of_the_previous_release_seeding_id_1_without_a_jurisdiction_is_the_iacs(pg):
    """During a rollout: the old code inserts (id=1, content, updated_at) only."""
    with pg.begin() as connection:
        connection.execute(text("INSERT INTO document_content (id, content, updated_at) VALUES (1, '{}', now())"))
    assert [(r[0], r[1]) for r in _rows(pg)] == [(1, IAC_ID)]


def test_the_iacs_wording_is_seeded_as_id_1_and_given_to_the_iac(pg):
    content_loader.get_document_content()
    assert [(r[0], r[1]) for r in _rows(pg)] == [(1, IAC_ID)]


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------

EDITED = {
    "hearing_types": [{"id": "face_to_face", "title": "Face to face — édité", "hearing_title": "H", "hearing_description": "D"}],
    "legal_frameworks": [{"id": "own", "title": "Own", "rank": 1, "content": "c \"quoted\" \\ ünï", "issues": ["i"]}],
    "anonymity": {"statuses": []},
}


def _migrate(engine, revision: str, *, down: bool = False) -> None:
    with engine.begin() as connection:
        config = migrations.alembic_config()
        config.attributes["connection"] = connection
        (command.downgrade if down else command.upgrade)(config, revision)


def _version(engine) -> str:
    with engine.connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def test_0006_keeps_the_iacs_row_unchanged_and_reverses():
    """An existing, edited IAC row survives upgrade and downgrade value for value: its id,
    content and updated_at (to the microsecond), which every instance's cache is keyed on."""
    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        _migrate(engine, "0005")
        assert _version(engine) == "0005"
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO document_content (id, content, updated_at)"
                    " VALUES (1, CAST(:content AS jsonb), '2026-03-04 05:06:07.123456+00')"
                ),
                {"content": json.dumps(EDITED)},
            )
        snapshot = "SELECT id, content::text, updated_at::text FROM document_content ORDER BY id"
        with engine.connect() as connection:
            before = connection.execute(text(snapshot)).all()
        assert len(before) == 1

        _migrate(engine, "head")
        assert _version(engine) == "0006"  # it ran
        with engine.connect() as connection:
            assert connection.execute(text(snapshot)).all() == before
            assert connection.execute(text("SELECT jurisdiction_id FROM document_content")).scalar_one() == IAC_ID

        # The application reads it as it is: no re-seed, no change.
        from transcribe_api.runtime import db_connection

        original = db_connection.get_engine
        db_connection.get_engine = lambda: engine
        try:
            content_loader.clear_cache()
            assert content_loader.get_document_content() == EDITED
        finally:
            db_connection.get_engine = original
            content_loader.clear_cache()
        with engine.connect() as connection:
            assert connection.execute(text(snapshot)).all() == before

        # Another jurisdiction's row goes with a downgrade; the IAC's is as it was.
        with engine.begin() as connection:
            et = _add_jurisdiction(engine, "ET")
            connection.execute(
                text("INSERT INTO document_content (jurisdiction_id, content, updated_at) VALUES (:j, '{}', now())"),
                {"j": et},
            )
        _migrate(engine, "0005", down=True)
        assert _version(engine) == "0005"
        with engine.connect() as connection:
            assert connection.execute(text(snapshot)).all() == before
            columns = connection.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'document_content'")
            ).scalars().all()
        assert sorted(columns) == ["content", "id", "updated_at"]

        # And it can be upgraded again.
        _migrate(engine, "head")
        with engine.connect() as connection:
            assert connection.execute(text(snapshot)).all() == before
    finally:
        engine.dispose()


def test_0006_on_a_database_without_the_row_creates_none_and_reserves_id_1():
    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        _migrate(engine, "0005")
        _migrate(engine, "head")
        assert _rows(engine) == []  # the first read seeds it, as before
        et = _add_jurisdiction(engine, "ET")
        with engine.begin() as connection:
            new_id = connection.execute(
                text("INSERT INTO document_content (jurisdiction_id, content, updated_at) VALUES (:j, '{}', now()) RETURNING id"),
                {"j": et},
            ).scalar_one()
        assert new_id == 2
    finally:
        engine.dispose()


def test_0006_after_rows_were_inserted_with_ids_keeps_numbering_from_2():
    """A sequence already past 1 (the old code never used it) is put back to 2."""
    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        _migrate(engine, "0005")
        with engine.begin() as connection:
            for _ in range(3):  # nextval used, rows deleted
                connection.execute(text("SELECT nextval('document_content_id_seq')"))
        _migrate(engine, "head")
        et = _add_jurisdiction(engine, "ET")
        with engine.begin() as connection:
            new_id = connection.execute(
                text("INSERT INTO document_content (jurisdiction_id, content, updated_at) VALUES (:j, '{}', now()) RETURNING id"),
                {"j": et},
            ).scalar_one()
        assert new_id == 2
    finally:
        engine.dispose()
