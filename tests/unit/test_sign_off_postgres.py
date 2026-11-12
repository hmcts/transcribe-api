"""DIAAT-347 on a real PostgreSQL (TEST_POSTGRES_URL, a throwaway one): the sign-off
guards of migration 0012, which SQLite would not run.

While this release rolls out, the previous release's pods still save jurisdictions
and templates. They know neither the sign-off nor a template's stage: they write a
jurisdiction's ``stage`` and a template's ``enabled`` alone. Their writes are the
plain UPDATEs here; this release's go through set_stage. Skipped without a database.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from tests.unit.template_docx import JUDGMENT, make_template
from transcribe_api import jurisdiction_templates
from transcribe_api.domain.models_dictation import Jurisdiction, JurisdictionTemplate
from transcribe_api.jurisdictions_admin import SignOff, set_stage

SIGNED_OFF = "SELECT stage, signed_off_by, signed_off_at IS NOT NULL FROM {table} WHERE id = :id"


def previous_release(engine, sql: str, **params) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params)


def state(engine, table: str, row_id) -> tuple:
    with engine.connect() as connection:
        return tuple(connection.execute(text(SIGNED_OFF.format(table=table)), {"id": row_id}).one())


def add_jurisdiction(engine, stage: str) -> Jurisdiction:
    with Session(engine) as session:
        row = Jurisdiction(code="ET", name="Employment Tribunal", settings={}, stage=stage)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def move(engine, model, row_id, stage: str, sign_off: SignOff | None = None) -> None:
    """This release's move: set_stage, and a template's ``enabled`` kept equal to live."""
    with Session(engine) as session:
        row = session.get(model, row_id)
        set_stage(row, stage, sign_off)
        if model is JurisdictionTemplate:
            row.enabled = stage == "live"
        session.add(row)
        session.commit()


def test_the_previous_release_cannot_put_a_jurisdiction_live(fresh_postgres):
    """It writes the stage alone, with no sign-off: from draft or test it is refused.
    One that was signed off and left live has lost that sign-off, so it is refused too."""
    et = add_jurisdiction(fresh_postgres, "test")
    with pytest.raises(IntegrityError, match="sign-off is needed to switch jurisdiction "):
        previous_release(fresh_postgres, "UPDATE jurisdiction SET stage = 'live' WHERE id = :id", id=et.id)
    assert state(fresh_postgres, "jurisdiction", et.id) == ("test", None, False)

    move(fresh_postgres, Jurisdiction, et.id, "live", SignOff("A Lead"))
    assert state(fresh_postgres, "jurisdiction", et.id) == ("live", "A Lead", True)
    previous_release(fresh_postgres, "UPDATE jurisdiction SET stage = 'test' WHERE id = :id", id=et.id)
    assert state(fresh_postgres, "jurisdiction", et.id) == ("test", None, False)
    with pytest.raises(IntegrityError, match="sign-off is needed to switch jurisdiction "):
        previous_release(fresh_postgres, "UPDATE jurisdiction SET stage = 'live' WHERE id = :id", id=et.id)


def test_a_jurisdiction_live_before_sign_offs_stays_live_and_is_still_saved(fresh_postgres):
    et = add_jurisdiction(fresh_postgres, "live")

    previous_release(fresh_postgres, "UPDATE jurisdiction SET name = 'ET', enabled = false WHERE id = :id", id=et.id)
    move(fresh_postgres, Jurisdiction, et.id, "live")

    assert state(fresh_postgres, "jurisdiction", et.id) == ("live", None, False)


def test_this_release_puts_a_jurisdiction_live_with_the_sign_off(fresh_postgres):
    et = add_jurisdiction(fresh_postgres, "draft")

    move(fresh_postgres, Jurisdiction, et.id, "test")
    move(fresh_postgres, Jurisdiction, et.id, "live", SignOff("A Lead"))

    assert state(fresh_postgres, "jurisdiction", et.id) == ("live", "A Lead", True)


def test_a_template_switched_off_by_the_previous_release_needs_a_fresh_sign_off(fresh_postgres):
    """Its pods switch a template off by ``enabled`` alone: it becomes a draft, its
    sign-off cleared, so switching it on again there is refused. This release puts it
    live again with a new sign-off."""
    add_jurisdiction(fresh_postgres, "live")
    with Session(fresh_postgres) as session:
        template_id = jurisdiction_templates.add_template(session, "ET", "ET judgment.docx", make_template(JUDGMENT)).id
    move(fresh_postgres, JurisdictionTemplate, template_id, "live", SignOff("A Lead"))
    assert state(fresh_postgres, "jurisdiction_template", template_id) == ("live", "A Lead", True)

    off = "UPDATE jurisdiction_template SET enabled = false WHERE id = :id"
    previous_release(fresh_postgres, off, id=template_id)
    assert state(fresh_postgres, "jurisdiction_template", template_id) == ("draft", None, False)
    with Session(fresh_postgres) as session:
        assert (
            jurisdiction_templates.template_record(session.get(JurisdictionTemplate, template_id))["stage"] == "draft"
        )

    on = "UPDATE jurisdiction_template SET enabled = true WHERE id = :id"
    with pytest.raises(IntegrityError, match="ck_jurisdiction_template_enabled_is_live"):
        previous_release(fresh_postgres, on, id=template_id)
    with pytest.raises(IntegrityError, match="sign-off is needed to switch jurisdiction_template "):
        previous_release(
            fresh_postgres,
            "UPDATE jurisdiction_template SET enabled = true, stage = 'live' WHERE id = :id",
            id=template_id,
        )

    move(fresh_postgres, JurisdictionTemplate, template_id, "live", SignOff("B Lead"))
    assert state(fresh_postgres, "jurisdiction_template", template_id) == ("live", "B Lead", True)
    with Session(fresh_postgres) as session:
        assert session.exec(select(JurisdictionTemplate.enabled)).one() is True
