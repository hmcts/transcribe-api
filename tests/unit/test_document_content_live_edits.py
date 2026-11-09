"""DIAAT-343: a deployment never overwrites wording edited in the live service.

The live wording is the document_content row (id=1). Nothing that runs at deployment may
replace it: not get_document_content(), which seeds the bundled defaults only when there
is no row, and not the migrations that run at startup. The tests against a real database
need TEST_POSTGRES_URL (a throwaway Postgres) and are skipped without it.
"""

from __future__ import annotations

import copy
import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from transcribe_api.documents import content_loader
from transcribe_api.documents.content_loader import _load_from_file
from transcribe_api.runtime import migrations

needs_postgres = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)"
)

EDITED_AT = "2026-03-04 05:06:07+00"


def _edited_content() -> dict:
    content = copy.deepcopy(_load_from_file())
    content["hearing_types"][0]["hearing_title"] = "Wording an admin changed in the live service"
    content["legal_frameworks"].pop()
    content["anonymity"]["statuses"][0]["content"] = "Anonymity wording an admin changed"
    return content


# Without a database ---------------------------------------------------------------------


class _Result:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class _FakeSession:
    """A document_content table of at most one row, which records every write."""

    def __init__(self, row, writes: list[str]):
        self.row = row
        self.writes = writes
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def exec(self, statement):
        # get_document_content selects updated_at first, then the whole row.
        self.reads += 1
        if self.row is not None and self.reads == 1:
            return _Result(self.row.updated_at)
        return _Result(self.row)

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    def execute(self, statement):
        # content_loader seeds with INSERT ... ON CONFLICT DO NOTHING.
        from transcribe_api.domain.models_dictation import DocumentContent

        self.writes.append("insert")
        if self.row is None:
            self.row = DocumentContent(**statement.compile().params)

    def commit(self):
        self.writes.append("commit")

    def rollback(self):
        self.writes.append("rollback")


@pytest.fixture(autouse=True)
def _fresh_cache():
    content_loader._cache.clear()
    yield
    content_loader._cache.clear()


def _table(monkeypatch, row, writes):
    monkeypatch.setattr("sqlmodel.Session", lambda engine: _FakeSession(row, writes))
    monkeypatch.setattr("transcribe_api.runtime.db_connection.get_engine", lambda: object())


def test_an_existing_row_is_returned_and_never_written(monkeypatch):
    from datetime import UTC, datetime

    from transcribe_api.domain.models_dictation import DocumentContent

    writes: list[str] = []
    edited = _edited_content()
    _table(monkeypatch, DocumentContent(id=1, content=edited, updated_at=datetime(2026, 3, 4, tzinfo=UTC)), writes)

    assert content_loader.get_document_content() == edited
    assert content_loader.get_document_content() == edited  # from the cache
    assert writes == []


def test_the_bundled_defaults_seed_only_an_empty_table(monkeypatch):
    writes: list[str] = []
    session = _FakeSession(None, writes)
    monkeypatch.setattr("sqlmodel.Session", lambda engine: session)
    monkeypatch.setattr("transcribe_api.runtime.db_connection.get_engine", lambda: object())

    assert content_loader.get_document_content() == _load_from_file()
    assert writes == ["insert", "commit"]
    assert session.row.id == 1


# With a real database -------------------------------------------------------------------


def _reset_database(engine) -> None:
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))


def _insert_edited_row(engine, content: dict) -> None:
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO document_content (id, content, updated_at) VALUES (1, CAST(:c AS jsonb), :t)"),
            {"c": json.dumps(content), "t": EDITED_AT},
        )


def _stored_rows(engine) -> list[tuple]:
    from sqlalchemy import text

    with engine.connect() as connection:
        rows = connection.execute(text("SELECT id, content, updated_at FROM document_content ORDER BY id")).all()
    return [tuple(r) for r in rows]


@pytest.fixture
def engine(monkeypatch):
    from sqlalchemy import create_engine

    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    _reset_database(engine)
    monkeypatch.setattr("transcribe_api.runtime.db_connection.get_engine", lambda: engine)
    yield engine
    engine.dispose()


@needs_postgres
def test_get_document_content_keeps_an_edited_row(engine):
    migrations.run_migrations(engine)
    edited = _edited_content()
    _insert_edited_row(engine, edited)
    before = _stored_rows(engine)

    assert content_loader.get_document_content() == edited
    assert _stored_rows(engine) == before
    assert len(before) == 1


@needs_postgres
def test_get_document_content_seeds_the_defaults_only_when_there_is_no_row(engine):
    migrations.run_migrations(engine)
    assert _stored_rows(engine) == []

    assert content_loader.get_document_content() == _load_from_file()
    [(row_id, content, _updated_at)] = _stored_rows(engine)
    assert (row_id, content) == (1, _load_from_file())


@needs_postgres
def test_migrations_keep_an_edited_row(engine):
    """A row written under 0001 survives the upgrade to head, and a second upgrade."""
    from alembic import command

    with engine.begin() as connection:
        config = migrations.alembic_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0001")
    edited = _edited_content()
    _insert_edited_row(engine, edited)

    migrations.run_migrations(engine)
    [(row_id, content, updated_at)] = _stored_rows(engine)
    assert (row_id, content) == (1, edited)
    assert updated_at.isoformat().startswith("2026-03-04T05:06:07")

    migrations.run_migrations(engine)
    assert _stored_rows(engine) == [(row_id, content, updated_at)]


@needs_postgres
def test_startup_keeps_an_edited_row_and_reports_the_difference(engine, monkeypatch, caplog):
    """The real startup path: migrate, check drift, serve a request that reads the content."""
    migrations.run_migrations(engine)
    edited = _edited_content()
    _insert_edited_row(engine, edited)
    before = _stored_rows(engine)
    monkeypatch.setenv("RUN_MIGRATIONS", "true")
    from transcribe_api.api.app import create_app

    with caplog.at_level("WARNING", logger="transcribe_api.documents.content_drift"), TestClient(create_app()):
        assert content_loader.get_document_content() == edited

    assert _stored_rows(engine) == before
    assert f"hearing_types/{_load_from_file()['hearing_types'][0]['id']}" in caplog.text
    assert "differs from the bundled defaults" in caplog.text
