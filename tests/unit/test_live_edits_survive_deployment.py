"""DIAAT-343: a deployment never overwrites what an admin changed in the live service,
and tells the product team where the live service differs from the shipped defaults.

Against a real database (fresh_postgres, TEST_POSTGRES_URL: a throwaway Postgres; skipped
without one): edit the live IAC jurisdiction row and the document_content row, run the
migrations again and start the app as a deployment does (RUN_MIGRATIONS=true), then
check that both rows, and their updated timestamps, are as the admin left them.
"""

from __future__ import annotations

import copy
import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from transcribe_api import jurisdictions_admin as admin
from transcribe_api.documents.content_loader import _load_from_file
from transcribe_api.runtime import migrations

CONTENT_EDITED_AT = "2026-03-04 05:06:07+00"
WORDING = "Wording an admin typed in the live service"


@pytest.fixture
def live_db(fresh_postgres, monkeypatch):
    monkeypatch.setattr("transcribe_api.runtime.db_connection.get_engine", lambda: fresh_postgres)
    monkeypatch.setenv("RUN_MIGRATIONS", "true")
    return fresh_postgres


@pytest.fixture
def events(monkeypatch) -> list[tuple]:
    recorded: list[tuple] = []
    monkeypatch.setattr(
        "hmcts_otel.record_event", lambda name, attributes=None, **kw: recorded.append((name, attributes))
    )
    return recorded


def _jurisdiction_rows(engine) -> list[tuple]:
    with engine.connect() as connection:
        return [
            tuple(r)
            for r in connection.execute(
                text(
                    "SELECT id, code, name, enabled, auto_enrol, settings, created_datetime, updated_datetime"
                    " FROM jurisdiction ORDER BY code"
                )
            )
        ]


def _content_rows(engine) -> list[tuple]:
    with engine.connect() as connection:
        return [tuple(r) for r in connection.execute(text("SELECT id, content, updated_at FROM document_content"))]


def _edit_the_live_iac(engine) -> None:
    """What the admin area does (set_wording), plus a flag and a key it cannot set."""
    from sqlmodel import Session

    with Session(engine) as session:
        wording, _version = admin.wording_and_version(session, "IAC")
        admin.set_wording(session, "IAC", {**wording, "home.intro": WORDING, "nav.recordings": "Recordings"})
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jurisdiction SET settings = jsonb_set(jsonb_set(settings, '{features,hearing_recordings}',"
                " 'false'), '{wording,added.by.hand}', to_jsonb(CAST(:text AS text))) WHERE code = 'IAC'"
            ),
            {"text": WORDING},
        )


def _edit_the_live_iac_at_0004(engine) -> None:
    """_edit_the_live_iac in SQL: this release's model has columns 0004 has not (0007)."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jurisdiction SET updated_datetime = now(), settings = jsonb_set(jsonb_set(jsonb_set(jsonb_set("
                "settings, '{wording,home.intro}', to_jsonb(CAST(:text AS text))),"
                " '{wording,nav.recordings}', '\"Recordings\"'),"
                " '{features,hearing_recordings}', 'false'), '{wording,added.by.hand}', to_jsonb(CAST(:text AS text)))"
                " WHERE code = 'IAC'"
            ),
            {"text": WORDING},
        )


def _edit_the_live_content(engine) -> dict:
    content = copy.deepcopy(_load_from_file())
    content["hearing_types"][0]["hearing_title"] = WORDING
    content["legal_frameworks"].pop()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO document_content (id, content, updated_at) VALUES (1, CAST(:c AS jsonb), :t)"),
            {"c": json.dumps(content), "t": CONTENT_EDITED_AT},
        )
    return content


def test_a_deployment_keeps_live_edits_and_reports_the_differences(live_db, events, caplog):
    _edit_the_live_iac(live_db)
    edited_content = _edit_the_live_content(live_db)
    jurisdictions_before, content_before = _jurisdiction_rows(live_db), _content_rows(live_db)
    [iac] = jurisdictions_before
    assert iac[5]["wording"]["home.intro"] == WORDING  # the edit is there to be kept
    assert iac[5]["features"] == {"hearing_recordings": False}
    assert content_before[0][2].isoformat().startswith("2026-03-04T05:06:07")

    from transcribe_api.api.app import create_app

    migrations.run_migrations(live_db)  # a later release's upgrade head: nothing to do, nothing rewritten
    with caplog.at_level(logging.WARNING):
        with TestClient(create_app()):  # and the startup of a pod: migrate, then the drift checks
            pass

    assert _jurisdiction_rows(live_db) == jurisdictions_before  # settings and updated_datetime
    assert _content_rows(live_db) == content_before  # content and updated_at
    assert content_before[0][1] == edited_content

    # The product team is told, by key name and count, never the wording.
    drift_events = {name: attributes for name, attributes in events}
    assert drift_events["settings.drift_detected"] == {
        "settings.wording_edited_count": 2,
        "settings.wording_live_only_count": 1,
        "settings.wording_default_only_count": 0,
        "settings.template_changed": False,
        "settings.features_changed_count": 1,
    }
    assert drift_events["content.drift_detected"] == {
        "content.edited_count": 1,
        "content.live_only_count": 0,
        "content.default_only_count": 1,
    }
    assert {r.name for r in caplog.records if r.levelno == logging.WARNING} >= {
        "transcribe_api.domain.jurisdiction_settings_drift",
        "transcribe_api.documents.content_drift",
    }
    assert "home.intro" in caplog.text
    assert "added.by.hand" in caplog.text
    assert "hearing_recordings" in caplog.text
    assert WORDING not in caplog.text
    assert WORDING not in str(events)


def test_a_deployment_with_nothing_edited_is_silent(live_db, events, caplog):
    jurisdictions_before = _jurisdiction_rows(live_db)
    from transcribe_api.api.app import create_app

    with caplog.at_level(logging.WARNING):
        with TestClient(create_app()):
            pass

    assert _jurisdiction_rows(live_db) == jurisdictions_before
    assert _content_rows(live_db) == []  # not seeded by the check
    assert events == []
    assert "differ" not in caplog.text


def test_the_migrations_a_release_brings_keep_edits_made_before_it(live_db):
    """Edit the rows at 0004, then upgrade to head: the later migrations (0005) rewrite neither."""
    from alembic import command

    with live_db.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        config = migrations.alembic_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0004")
    _edit_the_live_iac_at_0004(live_db)
    _edit_the_live_content(live_db)
    jurisdictions_before, content_before = _jurisdiction_rows(live_db), _content_rows(live_db)

    migrations.run_migrations(live_db)

    assert _jurisdiction_rows(live_db) == jurisdictions_before
    assert _content_rows(live_db) == content_before
