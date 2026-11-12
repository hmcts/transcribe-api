"""A jurisdiction's wording by its code, from the Jurisdictions screen (DIAAT-391, DIAAT-394).

``GET/PUT /api/admin/jurisdictions/{code}/wording``: a JurisdictionSuperAdministrator edits any
jurisdiction's, and nobody else does (DIAAT-406), a JurisdictionAdministrator included; the
version/conflict handling is ``jurisdictions_admin.set_wording``'s.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from tests.unit import auth_fixtures as af
from tests.unit.admin_fixtures import ADMIN, JUDGE, LTM, SUPER, make_world
from transcribe_api.domain import jurisdictions
from transcribe_api.domain.models_dictation import Jurisdiction

pytestmark = pytest.mark.real_jurisdictions


@pytest.fixture
def world(jurisdiction_db, monkeypatch):
    world = make_world(jurisdiction_db, monkeypatch)
    with Session(jurisdiction_db) as session:
        et = session.get(Jurisdiction, world.et.id)
        et.settings = {"wording": {"help.subtitle": "Employment Tribunal"}}
        session.add(et)
        session.commit()
    yield world
    af.unstub_entra()


@pytest.fixture
def cast(world):
    return type(
        "Cast",
        (),
        {
            "jsuper": world.person("Super", (None, SUPER)),
            "jadmin_iac": world.person("Admin IAC", ("IAC", ADMIN)),
            "jadmin_et": world.person("Admin ET", ("ET", ADMIN)),
            "sysadmin": world.person("Sysadmin", sysadmin=True),
            "ltm": world.person("Legal", ("ET", LTM)),
            "judge": world.person("Judge", ("ET", JUDGE)),
        },
    )


def stored(engine, code: str) -> dict:
    with Session(engine) as session:
        return session.exec(select(Jurisdiction).where(Jurisdiction.code == code)).one().settings


def test_a_super_administrator_reads_and_edits_any_jurisdictions_wording(world, cast):
    current = cast.jsuper.get("/api/admin/jurisdictions/et/wording", role=SUPER)

    assert current.status_code == 200
    body = current.json()
    assert set(body) == {"code", "name", "keys", "wording", "version"}
    assert body["code"] == "ET" and body["wording"] == {"help.subtitle": "Employment Tribunal"}
    assert body["keys"] == jurisdictions.WORDING_KEYS and body["version"]

    saved = cast.jsuper.put(
        "/api/admin/jurisdictions/ET/wording",
        role=SUPER,
        json={"wording": {"help.subtitle": "  Employment Tribunals  ", "home.intro": " "}, "version": body["version"]},
    )
    assert saved.status_code == 200
    assert saved.json()["wording"] == {"help.subtitle": "Employment Tribunals"}  # trimmed; blank is the default
    assert saved.json()["version"] != body["version"]
    assert stored(world.engine, "ET")["wording"] == {"help.subtitle": "Employment Tribunals"}
    # And the IAC's, in the same session of work.
    assert (
        cast.jsuper.put(
            "/api/admin/jurisdictions/IAC/wording", role=SUPER, json={"wording": {"home.intro": "Hi"}}
        ).status_code
        == 200
    )


def _permission_denied(response) -> bool:
    return response.status_code == 403 and response.json()["detail"]["code"] == "permission_denied"


def test_a_jurisdiction_administrator_edits_no_wording_not_even_its_own(world, cast):
    for code in ("ET", "IAC"):
        assert _permission_denied(
            cast.jadmin_et.get(f"/api/admin/jurisdictions/{code}/wording", role=ADMIN, jurisdiction="ET")
        )
        assert _permission_denied(
            cast.jadmin_et.put(
                f"/api/admin/jurisdictions/{code}/wording",
                role=ADMIN,
                jurisdiction="ET",
                json={"wording": {"home.intro": "Hello from the ET"}},
            )
        )
    assert "home.intro" not in stored(world.engine, "ET")["wording"]
    assert stored(world.engine, "IAC")["wording"].get("home.intro") != "Hello from the ET"


def test_a_stale_version_is_a_conflict_and_changes_nothing(world, cast):
    version = cast.jsuper.get("/api/admin/jurisdictions/ET/wording", role=SUPER).json()["version"]
    assert (
        cast.jsuper.put(
            "/api/admin/jurisdictions/ET/wording", role=SUPER, json={"wording": {"home.intro": "A"}, "version": version}
        ).status_code
        == 200
    )

    stale = cast.jsuper.put(
        "/api/admin/jurisdictions/ET/wording", role=SUPER, json={"wording": {"home.intro": "B"}, "version": version}
    )
    assert (
        stale.status_code == 409
        and stale.json()["detail"]["code"] == "edit_conflict"
        and "changed by someone else" in stale.json()["detail"]["message"]
    )
    assert stored(world.engine, "ET")["wording"]["home.intro"] == "A"


def test_an_unknown_key_is_a_400_and_an_unknown_jurisdiction_a_404(world, cast):
    bad = cast.jsuper.put("/api/admin/jurisdictions/ET/wording", role=SUPER, json={"wording": {"no.such.key": "x"}})
    assert bad.status_code == 400
    assert cast.jsuper.get("/api/admin/jurisdictions/NOPE/wording", role=SUPER).status_code == 404
    assert cast.jsuper.put("/api/admin/jurisdictions/NOPE/wording", role=SUPER, json={"wording": {}}).status_code == 404


def test_the_save_is_audited_with_the_keys_changed_and_never_the_text(world, cast):
    cast.jsuper.put("/api/admin/jurisdictions/ET/wording", role=SUPER, json={"wording": {"home.intro": "secret text"}})
    (row,) = world.audit("jurisdiction.edit")
    assert row.user_id == str(cast.jsuper.user_id)
    assert row.detail == {"code": "ET", "fields": ["wording"], "wording_keys_changed": ["help.subtitle", "home.intro"]}
    assert "secret text" not in str(row.detail)


@pytest.mark.parametrize(("who", "role", "at"), [("sysadmin", None, None), ("ltm", LTM, "ET"), ("judge", JUDGE, "ET")])
def test_nobody_else_edits_wording_by_code(world, cast, who, role, at):
    person = getattr(cast, who)
    assert person.get("/api/admin/jurisdictions/ET/wording", role=role, jurisdiction=at).status_code == 403
    assert (
        person.put(
            "/api/admin/jurisdictions/ET/wording", role=role, jurisdiction=at, json={"wording": {"home.intro": "x"}}
        ).status_code
        == 403
    )
    assert "home.intro" not in stored(world.engine, "ET")["wording"]


def test_a_judge_promoted_to_administrator_still_edits_no_wording_and_loses_the_role_when_demoted(world, cast):
    """The product team's flow end to end, by the real assignment endpoints."""
    created = cast.jsuper.post(
        "/api/admin/assignments",
        role=SUPER,
        json={"user_id": str(cast.judge.user_id), "jurisdiction": "ET", "role": ADMIN},
    )
    assert created.status_code == 201
    assert (
        cast.judge.get("/api/admin/users", role=ADMIN, jurisdiction="ET", params={"jurisdiction": "ET"}).status_code
        == 200
    )
    assert _permission_denied(
        cast.judge.put(
            "/api/admin/jurisdictions/ET/wording",
            role=ADMIN,
            jurisdiction="ET",
            json={"wording": {"home.intro": "Hello"}},
        )
    )
    assert "home.intro" not in stored(world.engine, "ET")["wording"]

    assert cast.jsuper.delete(f"/api/admin/assignments/{created.json()['id']}", role=SUPER).status_code == 204
    assert cast.judge.sign_in(("ET", ADMIN)).json()["me"]["active"] is None
