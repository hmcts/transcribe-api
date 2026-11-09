"""The feature flag admin endpoints and the flag cache (DIAAT-392).

A SystemAdministrator lists and sets the system flags; nobody else reads or changes them; a change is
audited with from, to and the roles acted as; ``GET /api/flags`` and the dictation off-switch see it at
once in the process that took it, because the 5-second cache is cleared by the change; with no row a
flag has its configured default.
"""

from __future__ import annotations

import pytest
from sqlmodel import SQLModel

from tests.unit import auth_fixtures as af
from tests.unit.admin_fixtures import ADMIN, JUDGE, SUPER, make_world
from transcribe_api.domain import feature_flags
from transcribe_api.domain.models_dictation import SystemFeatureFlag
from transcribe_api.runtime import settings_dictation

pytestmark = [pytest.mark.real_jurisdictions, pytest.mark.real_feature_flags]


@pytest.fixture
def world(jurisdiction_db, monkeypatch):
    SQLModel.metadata.create_all(jurisdiction_db, tables=[SystemFeatureFlag.__table__])
    monkeypatch.delenv("DICTATION_ENABLED", raising=False)
    settings_dictation.get_settings.cache_clear()
    yield make_world(jurisdiction_db, monkeypatch)
    af.unstub_entra()
    settings_dictation.get_settings.cache_clear()


@pytest.fixture
def cast(world):
    return type(
        "Cast",
        (),
        {
            "sysadmin": world.person("Sysadmin", sysadmin=True, email="sys@justice.gov.uk"),
            "jsuper": world.person("Super", (None, SUPER)),
            "jadmin": world.person("Admin", ("IAC", ADMIN)),
            "judge": world.person("Judge", ("IAC", JUDGE)),
            "nobody": world.person("Nobody"),
        },
    )


def flag(listing: dict, name: str = "dictation_enabled") -> dict:
    return next(f for f in listing["flags"] if f["name"] == name)


# --- reading ----------------------------------------------------------------------------------------------


def test_the_list_has_the_contracts_shape_and_the_default_when_nothing_is_stored(world, cast):
    response = cast.sysadmin.get("/api/admin/feature-flags")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"flags"} and [f["name"] for f in body["flags"]] == ["dictation_enabled"]
    (dictation,) = body["flags"]
    assert set(dictation) == {"name", "value", "description", "updated_at", "updated_by"}
    assert dictation["value"] is True and dictation["description"]
    assert dictation["updated_at"] is None and dictation["updated_by"] is None


def test_the_default_comes_from_configuration(world, cast, monkeypatch):
    monkeypatch.setenv("DICTATION_ENABLED", "false")
    settings_dictation.get_settings.cache_clear()
    assert flag(cast.sysadmin.get("/api/admin/feature-flags").json())["value"] is False
    assert world.client.get("/api/flags", headers=cast.judge.headers(JUDGE, "IAC")).json() == {"dictation_enabled": False}


# --- setting ---------------------------------------------------------------------------------------------


def test_setting_a_flag_returns_the_list_as_the_get_gives_it(world, cast):
    response = cast.sysadmin.put("/api/admin/feature-flags/dictation_enabled", json={"value": False})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"flags"}
    assert flag(body)["value"] is False
    assert flag(body)["updated_by"] == "sys@justice.gov.uk" and flag(body)["updated_at"]
    assert cast.sysadmin.get("/api/admin/feature-flags").json() == body

    # And back, as many times as they like.
    again = cast.sysadmin.put("/api/admin/feature-flags/dictation_enabled", json={"value": True}).json()
    assert flag(again)["value"] is True


def test_a_change_takes_effect_at_once_in_api_flags_despite_the_cache(world, cast, monkeypatch):
    monkeypatch.setattr(feature_flags, "CACHE_TTL_SECONDS", 60.0)  # a long one: only an invalidation can explain it
    headers = cast.judge.headers(JUDGE, "IAC")
    assert world.client.get("/api/flags", headers=headers).json() == {"dictation_enabled": True}
    assert world.client.get("/api/flags", headers=headers).json() == {"dictation_enabled": True}  # cached

    assert cast.sysadmin.put("/api/admin/feature-flags/dictation_enabled", json={"value": False}).status_code == 200
    assert world.client.get("/api/flags", headers=headers).json() == {"dictation_enabled": False}
    assert feature_flags.is_dictation_enabled() is False

    cast.sysadmin.put("/api/admin/feature-flags/dictation_enabled", json={"value": True})
    assert world.client.get("/api/flags", headers=headers).json() == {"dictation_enabled": True}


def test_a_changed_flag_switches_the_dictation_routes_off_and_on(world, cast, monkeypatch):
    """The off-switch (DIAAT-388) reads the same cached flag: a route only a Judge's dictation permission lets in."""
    monkeypatch.setattr(feature_flags, "CACHE_TTL_SECONDS", 60.0)
    headers = cast.judge.headers(JUDGE, "IAC")
    first = world.client.get("/api/get-speech-token", headers=headers)
    assert first.status_code != 403 or first.json()["detail"]["code"] != "dictation_disabled"

    cast.sysadmin.put("/api/admin/feature-flags/dictation_enabled", json={"value": False})
    refused = world.client.get("/api/get-speech-token", headers=headers)
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "dictation_disabled"

    cast.sysadmin.put("/api/admin/feature-flags/dictation_enabled", json={"value": True})
    again = world.client.get("/api/get-speech-token", headers=headers)
    assert again.status_code != 403 or again.json()["detail"]["code"] != "dictation_disabled"


def test_an_unknown_flag_is_a_404_and_nothing_is_stored(world, cast):
    response = cast.sysadmin.put("/api/admin/feature-flags/no_such_flag", json={"value": True})
    assert response.status_code == 404 and response.json()["detail"]["code"] == "flag_not_found"
    assert [f["name"] for f in cast.sysadmin.get("/api/admin/feature-flags").json()["flags"]] == ["dictation_enabled"]
    assert world.audit("feature_flag.changed") == []


@pytest.mark.parametrize("body", [{"value": "true"}, {"value": 1}, {"value": 0}, {"value": None}, {"value": "no"}, {}, []])
def test_the_value_must_be_a_boolean(world, cast, body):
    response = cast.sysadmin.put("/api/admin/feature-flags/dictation_enabled", json=body)
    assert response.status_code == 422
    assert flag(cast.sysadmin.get("/api/admin/feature-flags").json())["value"] is True
    assert world.audit("feature_flag.changed") == []


# --- who ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("who", "role", "at"),
    [("jsuper", SUPER, None), ("jadmin", ADMIN, "IAC"), ("judge", JUDGE, "IAC"), ("nobody", None, None)],
)
def test_no_other_role_reads_or_changes_flags(world, cast, who, role, at):
    person = getattr(cast, who)
    assert person.get("/api/admin/feature-flags", role=role, jurisdiction=at).status_code == 403
    assert person.put("/api/admin/feature-flags/dictation_enabled", role=role, jurisdiction=at, json={"value": False}).status_code == 403
    assert flag(cast.sysadmin.get("/api/admin/feature-flags").json())["value"] is True
    assert world.audit("feature_flag.changed") == []


def test_the_system_administrator_works_whatever_role_they_act_as(world):
    both = world.person("Sysadmin Judge", ("IAC", JUDGE), sysadmin=True)
    assert both.get("/api/admin/feature-flags", role=JUDGE, jurisdiction="IAC").status_code == 200
    assert both.put("/api/admin/feature-flags/dictation_enabled", role=JUDGE, jurisdiction="IAC", json={"value": False}).status_code == 200


# --- audit --------------------------------------------------------------------------------------------------


def test_a_change_is_audited_with_from_to_and_the_roles_acted_as(world, cast):
    cast.sysadmin.put("/api/admin/feature-flags/dictation_enabled", json={"value": False})
    cast.sysadmin.put("/api/admin/feature-flags/dictation_enabled", json={"value": True})

    first, second = world.audit("feature_flag.changed")
    assert first.user_id == str(cast.sysadmin.user_id) and first.outcome == "success"
    assert first.detail == {"flag": "dictation_enabled", "from": True, "to": False, "acted_as": ["SystemAdministrator"]}
    assert second.detail["from"] is False and second.detail["to"] is True
    assert first.active_role is None


def test_the_active_role_is_in_the_audit_when_the_system_administrator_acts_as_one(world):
    both = world.person("Sysadmin Judge", ("IAC", JUDGE), sysadmin=True)
    both.put("/api/admin/feature-flags/dictation_enabled", role=JUDGE, jurisdiction="IAC", json={"value": False})

    (row,) = world.audit("feature_flag.changed")
    assert row.active_role == JUDGE and row.active_jurisdiction_id == world.iac.id
    assert row.detail["acted_as"] == [JUDGE, "SystemAdministrator"]


def test_setting_the_value_it_already_has_is_audited_as_no_change_of_value(world, cast):
    cast.sysadmin.put("/api/admin/feature-flags/dictation_enabled", json={"value": True})
    (row,) = world.audit("feature_flag.changed")
    assert row.detail["from"] is True and row.detail["to"] is True


# --- the cache ------------------------------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def reads(monkeypatch):
    """Count the database reads behind is_dictation_enabled(), with a clock the test moves."""
    clock = _Clock()
    monkeypatch.setattr(feature_flags.time, "monotonic", clock)
    monkeypatch.setattr(feature_flags, "CACHE_TTL_SECONDS", 5.0)
    feature_flags.invalidate_cache()
    seen = {"count": 0, "value": None, "readable": True}

    def stored(name):
        seen["count"] += 1
        return (seen["value"], seen["readable"])

    monkeypatch.setattr(feature_flags, "_stored_value", stored)
    seen["clock"] = clock
    return seen


def test_the_flag_is_read_from_the_database_once_per_ttl(reads):
    assert feature_flags.is_dictation_enabled() is True
    for _ in range(50):
        assert feature_flags.is_dictation_enabled() is True
    assert reads["count"] == 1

    reads["clock"].now += 4.9
    feature_flags.is_dictation_enabled()
    assert reads["count"] == 1
    reads["clock"].now += 0.2  # past the 5 seconds
    reads["value"] = False
    assert feature_flags.is_dictation_enabled() is False
    assert reads["count"] == 2


def test_a_change_made_here_clears_the_cache_at_once(reads, jurisdiction_db, monkeypatch):
    SQLModel.metadata.create_all(jurisdiction_db, tables=[SystemFeatureFlag.__table__])
    monkeypatch.setattr(feature_flags, "get_engine", lambda: jurisdiction_db)
    assert feature_flags.is_dictation_enabled() is True
    reads["value"] = False  # what the table will say
    assert feature_flags.is_dictation_enabled() is True  # still cached
    from sqlmodel import Session

    with Session(jurisdiction_db) as session:
        before = feature_flags.set_flag(session, "dictation_enabled", False, updated_by=None)
    assert before is True
    assert feature_flags.is_dictation_enabled() is False  # read again, not waited for
    assert reads["count"] == 2


def test_an_unreadable_database_gives_the_default_and_is_not_cached(reads):
    reads["readable"] = False
    assert feature_flags.is_dictation_enabled() is True
    assert feature_flags.is_dictation_enabled() is True
    assert reads["count"] == 2  # tried again each time

    reads["readable"] = True
    reads["value"] = False
    assert feature_flags.is_dictation_enabled() is False  # and picks the row up as soon as it can


def test_a_value_that_is_just_the_default_is_cached_too(reads):
    reads["value"] = None  # no row
    for _ in range(5):
        assert feature_flags.is_dictation_enabled() is True
    assert reads["count"] == 1


def test_the_cache_is_per_flag_name_and_unknown_names_are_an_error(reads):
    with pytest.raises(KeyError):
        feature_flags.flag_value("nope")
    assert reads["count"] == 0
