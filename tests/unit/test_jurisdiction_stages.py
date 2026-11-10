"""DIAAT-345: a new jurisdiction is set up as a draft, tried by a test group, then
switched on for everyone, without a release. Real lookups on SQLite."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlmodel import Session, select

from tests.unit.jurisdiction_fixtures import add_jurisdiction, add_member, add_user, client_as, sign_up
from tests.unit.test_migrations import _offline_sql
from transcribe_api import jurisdictions_admin as admin
from transcribe_api.domain import jurisdictions
from transcribe_api.domain.models_dictation import AuditLog, Jurisdiction

pytestmark = pytest.mark.real_jurisdictions

ET_SETTINGS = json.loads(
    (Path(__file__).resolve().parents[2] / "docs" / "jurisdictions" / "et.example.json").read_text()
)
ET = {"name": "Employment Tribunals", "settings": ET_SETTINGS}


@pytest.fixture
def admin_app(jurisdiction_db, monkeypatch):
    from transcribe_api.api import routes_dictation_privileged

    monkeypatch.setattr(routes_dictation_privileged, "get_engine", lambda: jurisdiction_db)
    app = FastAPI()
    app.include_router(routes_dictation_privileged.router, prefix="/api")
    return app


@pytest.fixture
def as_admin(jurisdiction_db, admin_app):
    admin_user = add_user(jurisdiction_db, "admin@example.test", roles=("SystemAdministrator",))
    add_member(jurisdiction_db, admin_user, add_jurisdiction(jurisdiction_db, "IAC", auto_enrol=True), role="admin")
    return client_as(admin_app, jurisdiction_db, admin_user)


def _codes(user) -> list[str]:
    return [m.code for m in jurisdictions.load_memberships(user.id)]


def _et(as_admin) -> dict:
    return next(j for j in as_admin.get("/api/admin/jurisdictions").json() if j["code"] == "ET")


def _members(as_admin, *people: tuple[str, str, bool]) -> None:
    for email, role, tester in people:
        added = as_admin.post(
            "/api/admin/jurisdictions/ET/members", json={"email": email, "role": role, "tester": tester}
        )
        assert added.status_code == 201


# Who sees it at each stage ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("stage", "role", "tester", "sees"),
    [
        ("draft", "admin", False, True),
        ("draft", "judge", True, False),
        ("draft", "judge", False, False),
        ("test", "admin", False, True),
        ("test", "judge", True, True),
        ("test", "judge", False, False),
        ("live", "judge", False, True),
        ("live", "admin", True, True),
    ],
)
def test_who_sees_a_jurisdiction_at_each_stage(stage, role, tester, sees):
    assert jurisdictions.sees(stage, role, tester) is sees


def test_a_new_jurisdiction_is_a_draft_only_its_admins_see(jurisdiction_db, as_admin):
    created = as_admin.put("/api/admin/jurisdictions/ET", json=ET)
    assert created.status_code == 200
    assert created.json()["stage"] == "draft"
    _members(as_admin, ("tester@example.test", "judge", True), ("lead@example.test", "admin", False))

    assert _codes(sign_up(jurisdiction_db, "tester@example.test")) == []
    assert _codes(sign_up(jurisdiction_db, "lead@example.test")) == ["ET"]


def test_in_test_only_the_test_group_and_admins_see_it(jurisdiction_db, as_admin, jurisdiction_app):
    as_admin.put("/api/admin/jurisdictions/ET", json=ET)
    _members(
        as_admin,
        ("tester@example.test", "judge", True),
        ("judge@example.test", "judge", False),
        ("lead@example.test", "admin", False),
    )
    moved = as_admin.put("/api/admin/jurisdictions/ET", json={**ET, "stage": "test"})
    assert moved.status_code == 200, moved.json()

    tester = sign_up(jurisdiction_db, "tester@example.test")
    judge = sign_up(jurisdiction_db, "judge@example.test")
    assert _codes(tester) == ["ET"]
    assert _codes(sign_up(jurisdiction_db, "lead@example.test")) == ["ET"]
    assert _codes(judge) == []
    # A judge outside the test group cannot reach it by naming it either.
    asked = client_as(jurisdiction_app, jurisdiction_db, judge).get(
        "/api/transcriptions-metadata", headers={"X-Jurisdiction": "ET"}
    )
    assert asked.status_code == 403
    me = client_as(jurisdiction_app, jurisdiction_db, tester).get("/api/jurisdictions/me").json()
    assert me["current"]["stage"] == "test"


def test_live_every_member_sees_it(jurisdiction_db, as_admin):
    as_admin.put("/api/admin/jurisdictions/ET", json=ET)
    _members(as_admin, ("judge@example.test", "judge", False), ("lead@example.test", "admin", False))
    assert as_admin.put("/api/admin/jurisdictions/ET", json={**ET, "stage": "test"}).status_code == 200
    judge = sign_up(jurisdiction_db, "judge@example.test")
    assert _codes(judge) == []

    assert as_admin.put("/api/admin/jurisdictions/ET", json={**ET, "stage": "live"}).status_code == 200
    assert _codes(judge) == ["ET"]


def test_switched_off_hides_it_whatever_its_stage(jurisdiction_db):
    et = add_jurisdiction(jurisdiction_db, "ET", enabled=False, stage="live")
    judge = add_user(jurisdiction_db)
    add_member(jurisdiction_db, judge, et, role="admin", tester=True)
    assert _codes(judge) == []


def test_new_users_join_only_a_live_auto_enrol_jurisdiction(jurisdiction_db):
    add_jurisdiction(jurisdiction_db, "IAC", auto_enrol=True)
    add_jurisdiction(jurisdiction_db, "ET", auto_enrol=True, stage="test")
    newcomer = sign_up(jurisdiction_db, "new@example.test")
    assert _codes(newcomer) == ["IAC"]
    with Session(jurisdiction_db) as session:
        et = session.exec(select(Jurisdiction).where(Jurisdiction.code == "ET")).one()
        assert admin.members_of(session, et.code) == []


# Switching on needs it to be ready -------------------------------------------------------


def test_missing_screen_wording_blocks_switching_on_and_says_what_is_missing(as_admin):
    as_admin.put("/api/admin/jurisdictions/ET", json={"name": "Employment Tribunals", "settings": {}})
    _members(as_admin, ("lead@example.test", "admin", False))

    refused = as_admin.put(
        "/api/admin/jurisdictions/ET",
        json={"name": "Employment Tribunals", "settings": {"wording": {"home.intro": "Hello"}}, "stage": "test"},
    )

    assert refused.status_code == 400
    detail = refused.json()["detail"]
    assert detail["code"] == "jurisdiction_not_ready"
    assert detail["problems"] == [
        (
            "Add the screen wording for: Subtitle of the help page; Title column of the documents list; "
            "Type column of the documents list, and its filter; What dictation is saved to, e.g. 'case record'."
        )
    ]
    assert detail["message"].startswith("ET is not ready to be switched on.")
    # Nothing was saved: still a draft, without the wording sent.
    et = _et(as_admin)
    assert (et["stage"], et["settings"]["wording"]) == ("draft", {})


def test_hearing_recordings_also_need_their_navigation_wording():
    settings = jurisdictions.JurisdictionSettings.model_validate(
        {**ET_SETTINGS, "features": {"hearing_recordings": True}}
    )
    assert jurisdictions.missing_wording(settings) == ["nav.recordings"]
    assert jurisdictions.missing_wording(jurisdictions.JurisdictionSettings.model_validate(ET_SETTINGS)) == []


def test_switching_on_needs_a_jurisdiction_admin(as_admin):
    as_admin.put("/api/admin/jurisdictions/ET", json=ET)

    refused = as_admin.put("/api/admin/jurisdictions/ET", json={**ET, "stage": "live"})

    assert refused.status_code == 400
    assert refused.json()["detail"]["problems"] == ["Assign at least one jurisdiction admin."]


def test_a_jurisdiction_cannot_be_created_switched_on(as_admin):
    refused = as_admin.put("/api/admin/jurisdictions/ET", json={**ET, "stage": "test"})
    assert refused.status_code == 400
    assert all(j["code"] != "ET" for j in as_admin.get("/api/admin/jurisdictions").json())


def test_the_list_says_what_stops_each_draft_being_switched_on(as_admin):
    as_admin.put("/api/admin/jurisdictions/ET", json={"name": "Employment Tribunals", "settings": {}})
    assert len(_et(as_admin)["problems"]) == 2

    as_admin.put("/api/admin/jurisdictions/ET", json=ET)
    _members(as_admin, ("lead@example.test", "admin", False))
    assert _et(as_admin)["problems"] == []


def test_a_jurisdiction_already_on_is_not_held_to_the_check(jurisdiction_db, as_admin):
    """Jurisdictions from before stages are live; editing one is not refused for the
    wording it never had (only switching on is: DIAAT-345)."""
    add_jurisdiction(jurisdiction_db, "OLD", settings={})
    saved = as_admin.put("/api/admin/jurisdictions/OLD", json={"name": "Old", "settings": {}})
    assert saved.status_code == 200
    assert saved.json()["stage"] == "live"


def test_a_jurisdiction_can_go_back_to_draft(jurisdiction_db, as_admin):
    as_admin.put("/api/admin/jurisdictions/ET", json=ET)
    _members(as_admin, ("lead@example.test", "admin", False), ("judge@example.test", "judge", True))
    as_admin.put("/api/admin/jurisdictions/ET", json={**ET, "stage": "test"})
    judge = sign_up(jurisdiction_db, "judge@example.test")
    assert _codes(judge) == ["ET"]

    assert as_admin.put("/api/admin/jurisdictions/ET", json={**ET, "stage": "draft"}).status_code == 200
    assert _codes(judge) == []


def test_an_unknown_stage_is_refused(as_admin, jurisdiction_db):
    assert as_admin.put("/api/admin/jurisdictions/ET", json={**ET, "stage": "beta"}).status_code == 422
    with Session(jurisdiction_db) as session, pytest.raises(admin.AdminError, match="stage must be one of"):
        admin.put_jurisdiction(session, "ET", "ET", {}, stage="beta")


# Display name, service and the test group -------------------------------------------------


def test_the_display_name_is_what_users_see_and_the_service_is_kept(jurisdiction_db, as_admin, jurisdiction_app):
    as_admin.put("/api/admin/jurisdictions/ET", json=ET)
    _members(as_admin, ("lead@example.test", "admin", False))
    saved = as_admin.put(
        "/api/admin/jurisdictions/ET",
        json={**ET, "display_name": " Employment Tribunals (ET) ", "service": "England and Wales"},
    ).json()
    assert (saved["name"], saved["display_name"], saved["service"]) == (
        "Employment Tribunals",
        "Employment Tribunals (ET)",
        "England and Wales",
    )

    lead = sign_up(jurisdiction_db, "lead@example.test")
    me = client_as(jurisdiction_app, jurisdiction_db, lead).get("/api/jurisdictions/me").json()
    assert me["current"]["name"] == "Employment Tribunals (ET)"
    assert me["jurisdictions"] == [
        {"code": "ET", "name": "Employment Tribunals (ET)", "role": "admin", "stage": "draft"}
    ]

    # Left out: kept. Blank: cleared, and the name is shown again.
    assert as_admin.put("/api/admin/jurisdictions/ET", json=ET).json()["display_name"] == "Employment Tribunals (ET)"
    cleared = as_admin.put("/api/admin/jurisdictions/ET", json={**ET, "display_name": "", "service": " "}).json()
    assert (cleared["display_name"], cleared["service"]) == (None, None)


def test_the_test_group_is_kept_when_only_the_role_changes(as_admin):
    as_admin.put("/api/admin/jurisdictions/ET", json=ET)
    _members(as_admin, ("judge@example.test", "judge", True))

    changed = as_admin.post(
        "/api/admin/jurisdictions/ET/members", json={"email": "judge@example.test", "role": "admin"}
    )
    assert (changed.json()["role"], changed.json()["tester"]) == ("admin", True)
    removed = as_admin.post(
        "/api/admin/jurisdictions/ET/members", json={"email": "judge@example.test", "role": "admin", "tester": False}
    )
    assert removed.json()["tester"] is False


def test_the_audit_records_the_stage_and_the_test_group(jurisdiction_db, as_admin):
    as_admin.put("/api/admin/jurisdictions/ET", json={**ET, "display_name": "ET"})
    _members(as_admin, ("judge@example.test", "judge", True))

    with Session(jurisdiction_db) as session:
        rows = session.exec(select(AuditLog)).all()
    [edit] = [r.detail for r in rows if r.event_type == "jurisdiction.edit"]
    [added] = [r.detail for r in rows if r.event_type == "membership.add"]
    assert (edit["stage"], edit["display_name"], edit["service"]) == ("draft", "ET", None)
    assert added["tester"] is True


def test_the_command_line_sets_the_stage_names_and_test_group(jurisdiction_db, monkeypatch):
    import io

    with Session(jurisdiction_db) as session:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(ET_SETTINGS)))
        admin.run(["put", "ET", "--name", "Employment Tribunals", "--display-name", "ET (E&W)"], session)
        admin.run(["add-member", "ET", "lead@example.test", "--role", "admin", "--tester"], session)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(ET_SETTINGS)))
        admin.run(["put", "ET", "--name", "Employment Tribunals", "--stage", "test"], session)

        assert admin.run(["list"], session) == "ET\tEmployment Tribunals\ttest\t1 member(s)\tenabled"
        row = session.exec(select(Jurisdiction).where(Jurisdiction.code == "ET")).one()
        assert row.display_name == "ET (E&W)"  # kept: not given the second time
        assert admin.members_of(session, "ET")[0]["tester"] is True


# Migration 0007 ----------------------------------------------------------------------------


def test_0007_adds_the_columns_and_existing_jurisdictions_stay_live(monkeypatch):
    sql = _offline_sql(monkeypatch, "0006:0007")
    assert re.search(r"ALTER TABLE jurisdiction ADD COLUMN stage VARCHAR\(8\) DEFAULT 'live' NOT NULL", sql)
    assert "CHECK (stage IN ('draft', 'test', 'live'))" in sql
    assert "ALTER TABLE jurisdiction ADD COLUMN display_name VARCHAR" in sql
    assert "ALTER TABLE jurisdiction ADD COLUMN service VARCHAR" in sql
    assert re.search(r"ALTER TABLE jurisdiction_membership ADD COLUMN tester BOOLEAN DEFAULT false NOT NULL", sql)


def test_0007_downgrade_switches_off_what_was_not_live(monkeypatch):
    sql = _offline_sql(monkeypatch, "0007:0006", downgrade=True)
    assert sql.index("UPDATE jurisdiction SET enabled = false WHERE stage <> 'live'") < sql.index(
        "ALTER TABLE jurisdiction DROP COLUMN stage"
    )


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="needs TEST_POSTGRES_URL (a throwaway Postgres)")
def test_0007_on_postgres_keeps_every_jurisdiction_as_it_was_and_reverses():
    """Existing jurisdictions and members come out live and not in a test group, their
    rows otherwise untouched; a downgrade switches off whatever was not live."""
    from sqlalchemy import create_engine, text

    from alembic import command
    from transcribe_api.runtime import migrations

    def migrate(revision: str, *, down: bool = False) -> None:
        with engine.begin() as connection:
            config = migrations.alembic_config()
            config.attributes["connection"] = connection
            (command.downgrade if down else command.upgrade)(config, revision)

    snapshot = (
        "SELECT code, name, enabled, auto_enrol, settings::text, updated_datetime::text FROM jurisdiction ORDER BY code"
    )
    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        migrate("0006")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO jurisdiction (id, code, name, enabled, auto_enrol, settings, created_datetime,"
                    " updated_datetime) VALUES (gen_random_uuid(), 'ET', 'ET', true, false, '{}', now(),"
                    " '2026-01-02 03:04:05.123456+00')"
                )
            )
            before = connection.execute(text(snapshot)).all()

        migrate("0007")
        with engine.begin() as connection:
            assert connection.execute(text(snapshot)).all() == before
            assert connection.execute(text("SELECT code, stage FROM jurisdiction ORDER BY code")).all() == [
                ("ET", "live"),
                ("IAC", "live"),
            ]
            assert connection.execute(text("SELECT bool_or(tester) FROM jurisdiction_membership")).scalar() in (
                None,
                False,
            )
            connection.execute(text("UPDATE jurisdiction SET stage = 'test' WHERE code = 'ET'"))

        migrate("0006", down=True)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT code, enabled FROM jurisdiction ORDER BY code")).all() == [
                ("ET", False),
                ("IAC", True),
            ]
    finally:
        engine.dispose()
