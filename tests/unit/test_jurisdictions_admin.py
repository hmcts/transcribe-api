"""DIAAT-322 "manual first": adding jurisdictions and members without a deployment."""

from __future__ import annotations

import io
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from transcribe_api import jurisdictions_admin as admin
from transcribe_api.domain.jurisdictions import JurisdictionSettings
from transcribe_api.domain.models_dictation import Jurisdiction, RoleAssignment, User

pytestmark = pytest.mark.real_jurisdictions

ET_EXAMPLE = Path(__file__).resolve().parents[2] / "docs" / "jurisdictions" / "et.example.json"


@pytest.fixture
def put(monkeypatch):
    """Run ``put`` from the command line with the ET example settings on standard input."""

    def run(session, *args: str) -> str:
        monkeypatch.setattr("sys.stdin", io.StringIO(ET_EXAMPLE.read_text()))
        return admin.run(["put", *args], session)

    return run


@pytest.fixture
def session(jurisdiction_db):
    with Session(jurisdiction_db) as session:
        yield session


def _user(session, email: str) -> User:
    user = User(email=email, azure_user_id=str(uuid4()))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


_MEMBER_ROLE = {"Judge": "judge", "JurisdictionAdministrator": "admin"}


def _members(session) -> list[tuple]:
    """(jurisdiction, user, pending email, member role) of every assignment: members are
    role_assignment rows (DIAAT-382)."""
    rows = session.exec(
        select(Jurisdiction.code, RoleAssignment.user_id, RoleAssignment.pending_email, RoleAssignment.role).join(
            Jurisdiction, Jurisdiction.id == RoleAssignment.jurisdiction_id
        )
    ).all()
    return sorted(((code, user, email, _MEMBER_ROLE[role]) for code, user, email, role in rows), key=str)


def test_the_et_example_settings_are_valid():
    settings = JurisdictionSettings.model_validate(json.loads(ET_EXAMPLE.read_text()))
    assert settings.template is None
    assert settings.features.hearing_recordings is False


def test_put_creates_then_updates_a_jurisdiction(session, put):
    assert put(session, "et", "--name", "Employment Tribunal") == (
        "Created ET"
    )
    assert admin.put_jurisdiction(session, "ET", "Employment Tribunals", {}, enabled=False) == "Updated ET"

    [row] = session.exec(select(Jurisdiction)).all()
    # Nobody is enrolled automatically any more (DIAAT-373): a new jurisdiction never is.
    assert (row.code, row.name, row.auto_enrol, row.enabled, row.settings["wording"]) == (
        "ET",
        "Employment Tribunals",
        False,
        False,
        {},
    )



def test_put_opens_no_file_named_on_its_command_line(session, tmp_path):
    """The settings come only from standard input (Sonar pythonsecurity:S8707): a path
    on the command line is refused, not read."""
    named = tmp_path / "et.json"
    named.write_text(ET_EXAMPLE.read_text())

    with pytest.raises(SystemExit):
        admin.run(["put", "ET", "--name", "Employment Tribunal", "--settings", str(named)], session)
    assert session.exec(select(Jurisdiction)).all() == []

def test_put_keeps_the_flag_it_is_not_given(session, put):
    """Updating a jurisdiction's wording must not re-enable a disabled jurisdiction."""
    admin.put_jurisdiction(session, "IAC", "Immigration and Asylum Chamber", {})
    admin.put_jurisdiction(session, "OLD", "Retired", {}, enabled=False)

    put(session, "IAC", "--name", "Immigration and Asylum Chamber")
    put(session, "OLD", "--name", "Retired")

    assert {r.code: r.enabled for r in session.exec(select(Jurisdiction)).all()} == {"IAC": True, "OLD": False}
    put(session, "OLD", "--name", "Retired", "--enabled")
    put(session, "IAC", "--name", "IAC", "--no-enabled")
    assert {r.code: r.enabled for r in session.exec(select(Jurisdiction)).all()} == {"IAC": False, "OLD": True}


def test_there_is_no_auto_enrol_option_any_more(session, put):
    with pytest.raises(SystemExit):
        put(session, "ET", "--name", "Employment Tribunal", "--auto-enrol")


def test_put_refuses_invalid_settings_and_codes(session):
    with pytest.raises(admin.AdminError, match="Invalid settings"):
        admin.put_jurisdiction(session, "ET", "Employment Tribunal", {"wording": {"nope": "x"}})
    with pytest.raises(admin.AdminError, match="letters and digits"):
        admin.put_jurisdiction(session, "E T", "Employment Tribunal", {})
    assert session.exec(select(Jurisdiction)).all() == []


def test_a_member_who_has_signed_in_is_added_by_user(session):
    admin.put_jurisdiction(session, "ET", "Employment Tribunal", {})
    user = _user(session, "Judge.One@Example.test")

    assert "signed-in user" in admin.add_member(session, "et", " judge.one@example.TEST ", "admin")
    assert _members(session) == [("ET", user.id, None, "admin")]
    # Again: the role is updated, not a second row.
    assert admin.add_member(session, "ET", "judge.one@example.test", "judge").startswith("Updated judge")
    assert _members(session) == [("ET", user.id, None, "judge")]


def test_someone_who_has_not_signed_in_is_added_pending_by_email(session):
    admin.put_jurisdiction(session, "ET", "Employment Tribunal", {})
    assert "pending until they sign in" in admin.add_member(session, "ET", "New.Judge@Example.test")
    assert _members(session) == [("ET", None, "new.judge@example.test", "judge")]


def test_removing_a_member_removes_all_their_roles_there_and_only_there(session):
    admin.put_jurisdiction(session, "ET", "Employment Tribunal", {})
    admin.put_jurisdiction(session, "IAC", "Immigration and Asylum Chamber", {})
    _user(session, "judge@example.test")
    admin.add_member(session, "ET", "judge@example.test")
    admin.add_member(session, "IAC", "judge@example.test")
    # Also given another role there, which this tool does not name: it goes with the member.
    from transcribe_api.domain.assignments import add_assignment

    et = session.exec(select(Jurisdiction).where(Jurisdiction.code == "ET")).one()
    user = session.exec(select(User)).one()
    add_assignment(session, role="LegalTextManager", jurisdiction_id=et.id, user_id=user.id)
    session.commit()

    assert admin.remove_member(session, "ET", "judge@example.test") == "Removed from ET"
    assert [(code, role) for code, _user_id, _email, role in _members(session)] == [("IAC", "judge")]
    with pytest.raises(admin.AdminError, match="Not a member"):
        admin.remove_member(session, "ET", "judge@example.test")


def test_unknown_jurisdictions_and_roles_are_refused(session):
    with pytest.raises(admin.AdminError, match="No jurisdiction ET"):
        admin.add_member(session, "ET", "a@example.test")
    admin.put_jurisdiction(session, "ET", "Employment Tribunal", {})
    with pytest.raises(admin.AdminError, match="role"):
        admin.add_member(session, "ET", "a@example.test", "clerk")


def test_two_user_rows_with_one_email_are_one_person(session):
    """An Entra account deleted and recreated leaves two rows: no developer is needed
    to add or remove that judge (DIAAT-356)."""
    admin.put_jurisdiction(session, "ET", "Employment Tribunal", {})
    _user(session, "twin@example.test")
    _user(session, "TWIN@example.test")

    admin.add_member(session, "ET", "twin@example.test")
    assert admin.members_of(session, "ET") == [{"email": "twin@example.test", "role": "judge", "tester": False, "pending": False}]
    assert admin.remove_member(session, "ET", "Twin@example.test") == "Removed from ET"
    assert admin.members_of(session, "ET") == []


def test_list_shows_each_jurisdiction(session):
    admin.put_jurisdiction(session, "IAC", "Immigration and Asylum Chamber", {})
    admin.put_jurisdiction(session, "ET", "Employment Tribunal", {}, enabled=False)
    admin.add_member(session, "IAC", "a@example.test")
    assert admin.run(["list"], session).splitlines() == [
        "ET\tEmployment Tribunal\tdraft\t0 member(s)\tdisabled",
        "IAC\tImmigration and Asylum Chamber\tdraft\t1 member(s)\tenabled",
    ]


def test_main_reports_a_refusal_without_a_traceback(monkeypatch, capsys, session):
    monkeypatch.setattr("transcribe_api.runtime.db_connection.get_engine", lambda: session.get_bind())
    assert admin.main(["add-member", "ET", "a@example.test"]) == 1
    assert "Refused: No jurisdiction ET" in capsys.readouterr().err


def test_a_jurisdiction_created_by_someone_else_at_the_same_moment_is_a_conflict(session, monkeypatch):
    """Both saves saw no ET; the second to commit is told it exists, not given a 500."""
    real_commit = session.commit

    def commit_after_the_other_admin():
        with Session(session.get_bind()) as other:
            other.add(Jurisdiction(code="ET", name="Theirs", settings={}, auto_enrol=False, enabled=True))
            other.commit()
        monkeypatch.setattr(session, "commit", real_commit)
        real_commit()

    monkeypatch.setattr(session, "commit", commit_after_the_other_admin)
    with pytest.raises(admin.ConflictError, match="ET already exists"):
        admin.put_jurisdiction(session, "ET", "Mine", {}, expected_version="")
    assert admin.jurisdictions_overview(session)[0]["name"] == "Theirs"


@pytest.mark.parametrize("save", ["wording", "jurisdiction"])
def test_a_change_made_after_this_session_read_the_row_is_a_conflict(session, jurisdiction_db, save):
    """The version is checked against the row as it is once locked, not as this
    session read it before: else two saves of one version could both pass."""
    admin.put_jurisdiction(session, "ET", "Employment Tribunal", {})
    # This session holds the row as read now (an object it keeps, as a caller would).
    held = session.exec(select(Jurisdiction).where(Jurisdiction.code == "ET")).one()
    opened = admin.version(held)
    with Session(jurisdiction_db) as other:
        admin.set_wording(other, "ET", {"help.subtitle": "Theirs"}, expected_version=opened)

    with pytest.raises(admin.ConflictError):
        if save == "wording":
            admin.set_wording(session, "ET", {"help.subtitle": "Mine"}, expected_version=opened)
        else:
            admin.put_jurisdiction(session, "ET", "Employment Tribunal", {}, expected_version=opened)
    session.rollback()
    with Session(jurisdiction_db) as fresh:
        assert admin.wording_and_version(fresh, "ET")[0] == {"help.subtitle": "Theirs"}
