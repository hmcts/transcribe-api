"""The user list, deactivation and the role-change history (DIAAT-383, DIAAT-391, DIAAT-394).

Scope isolation between jurisdictions, the contract's list shape, the picker (every user who has
signed in can be found), pending rows, paging, deactivation with its refusals, and who reads which
part of the role-change history.
"""

from __future__ import annotations

import pytest

from tests.unit import auth_fixtures as af
from tests.unit.admin_fixtures import ADMIN, JUDGE, LTM, SUPER, Person, make_world

pytestmark = pytest.mark.real_jurisdictions


@pytest.fixture
def world(jurisdiction_db, monkeypatch):
    yield make_world(jurisdiction_db, monkeypatch)
    af.unstub_entra()


@pytest.fixture
def cast(world):
    return type(
        "Cast",
        (),
        {
            "jadmin_iac": world.person("Admin IAC", ("IAC", ADMIN)),
            "jadmin_et": world.person("Admin ET", ("ET", ADMIN)),
            "jsuper": world.person("Super", (None, SUPER)),
            "sysadmin": world.person("Sysadmin", sysadmin=True),
            "judge_iac": world.person("Ivy Judge", ("IAC", JUDGE)),
            "judge_et": world.person("Eve Judge", ("ET", JUDGE)),
            "both": world.person("Bea Both", ("IAC", JUDGE), ("ET", LTM)),
            "nobody": world.person("Nora Nobody"),
        },
    )


def emails(page: dict) -> list[str]:
    return [item["email"] for item in page["items"]]


def code(response) -> str:
    return response.json()["detail"]["code"]


# --- the list -------------------------------------------------------------------------------------------


def test_the_list_has_the_contracts_shape(world, cast):
    page = cast.jsuper.get("/api/admin/users", role=SUPER).json()

    assert set(page) == {"items", "total", "page", "page_size"}
    assert page["page"] == 1 and page["page_size"] == 25 and page["total"] == len(page["items"]) == 8
    for item in page["items"]:
        assert set(item) == {"id", "email", "name", "is_active", "assignments", "pending"}
        for assignment in item["assignments"]:
            assert set(assignment) == {"id", "jurisdiction", "role", "tester"}
    ivy = next(i for i in page["items"] if i["email"] == cast.judge_iac.email)
    assert ivy["id"] == str(cast.judge_iac.user_id) and ivy["name"] == "Ivy Judge"
    assert ivy["is_active"] is True and ivy["pending"] is False
    assert ivy["assignments"][0]["jurisdiction"] == {"code": "IAC", "name": "Immigration and Asylum Chamber"}
    assert ivy["assignments"][0]["role"] == JUDGE
    # The global role has no jurisdiction object.
    boss = next(i for i in page["items"] if i["email"] == cast.jsuper.email)
    assert boss["assignments"][0]["jurisdiction"] is None


def test_a_jurisdiction_administrator_lists_only_the_users_of_the_jurisdiction_it_acts_in(world, cast):
    page = cast.jadmin_iac.get("/api/admin/users", role=ADMIN, jurisdiction="IAC").json()

    assert sorted(emails(page)) == sorted([cast.jadmin_iac.email, cast.judge_iac.email, cast.both.email])
    assert page["total"] == 3
    # Only what they hold in IAC: a user who also works in ET shows the IAC assignment alone.
    both = next(i for i in page["items"] if i["email"] == cast.both.email)
    assert [(a["jurisdiction"]["code"], a["role"]) for a in both["assignments"]] == [("IAC", JUDGE)]

    other = cast.jadmin_et.get("/api/admin/users", role=ADMIN, jurisdiction="ET").json()
    assert sorted(emails(other)) == sorted([cast.jadmin_et.email, cast.judge_et.email, cast.both.email])
    both_et = next(i for i in other["items"] if i["email"] == cast.both.email)
    assert [(a["jurisdiction"]["code"], a["role"]) for a in both_et["assignments"]] == [("ET", LTM)]


def test_a_jurisdiction_administrator_may_not_ask_for_another_jurisdiction(world, cast):
    refused = cast.jadmin_iac.get("/api/admin/users", role=ADMIN, jurisdiction="IAC", params={"jurisdiction": "ET"})
    assert refused.status_code == 403 and code(refused) == "jurisdiction_not_in_scope"
    unknown = cast.jadmin_iac.get("/api/admin/users", role=ADMIN, jurisdiction="IAC", params={"jurisdiction": "NOPE"})
    assert unknown.status_code == 404


def test_a_search_finds_any_user_who_has_signed_in_so_they_can_be_added_but_shows_only_the_callers_jurisdiction(
    world, cast
):
    """The picker: a user with no role, or a role only elsewhere, is found by their email."""
    found = cast.jadmin_iac.get(
        "/api/admin/users", role=ADMIN, jurisdiction="IAC", params={"search": cast.nobody.email[:12]}
    ).json()
    assert emails(found) == [cast.nobody.email] and found["items"][0]["assignments"] == []
    assert found["items"][0]["id"] == str(cast.nobody.user_id)

    elsewhere = cast.jadmin_iac.get(
        "/api/admin/users", role=ADMIN, jurisdiction="IAC", params={"search": "eve.judge"}
    ).json()
    (eve,) = elsewhere["items"]
    assert eve["email"] == cast.judge_et.email
    # Their ET role is not the IAC administrator's to see.
    assert eve["assignments"] == []

    # Case does not matter, and what is not there is not found.
    assert emails(
        cast.jadmin_iac.get("/api/admin/users", role=ADMIN, jurisdiction="IAC", params={"search": "NORA.NOBODY"}).json()
    ) == [cast.nobody.email]
    assert cast.jadmin_iac.get(
        "/api/admin/users", role=ADMIN, jurisdiction="IAC", params={"search": "zzzzzz"}
    ).json()["items"] == []


def test_a_very_short_search_stays_inside_the_callers_jurisdiction(world, cast):
    """Two characters would list half the directory: that is not the picker, it is a scrape."""
    page = cast.jadmin_iac.get("/api/admin/users", role=ADMIN, jurisdiction="IAC", params={"search": "ev"}).json()
    assert page["items"] == []  # Eve (ET only) is not found by two letters
    page = cast.jadmin_iac.get("/api/admin/users", role=ADMIN, jurisdiction="IAC", params={"search": "iv"}).json()
    assert emails(page) == [cast.judge_iac.email]  # Ivy is in the jurisdiction


def test_the_jurisdiction_and_role_filters_are_hard_filters_even_for_a_search(world, cast):
    page = cast.jadmin_iac.get(
        "/api/admin/users",
        role=ADMIN,
        jurisdiction="IAC",
        params={"jurisdiction": "IAC", "search": "nora.nobody"},
    ).json()
    assert page["items"] == []
    judges = cast.jadmin_iac.get(
        "/api/admin/users", role=ADMIN, jurisdiction="IAC", params={"role": JUDGE}
    ).json()
    assert sorted(emails(judges)) == sorted([cast.judge_iac.email, cast.both.email])


def test_a_super_administrator_lists_everyone_and_can_filter_by_jurisdiction_and_role(world, cast):
    everyone = cast.jsuper.get("/api/admin/users", role=SUPER).json()
    assert everyone["total"] == 8

    et = cast.jsuper.get("/api/admin/users", role=SUPER, params={"jurisdiction": "ET"}).json()
    assert sorted(emails(et)) == sorted([cast.jadmin_et.email, cast.judge_et.email, cast.both.email])
    # Unfiltered by role, every assignment shows (also those elsewhere).
    both = next(i for i in et["items"] if i["email"] == cast.both.email)
    assert sorted((a["jurisdiction"]["code"], a["role"]) for a in both["assignments"]) == [("ET", LTM), ("IAC", JUDGE)]

    admins = cast.jsuper.get("/api/admin/users", role=SUPER, params={"role": ADMIN}).json()
    assert sorted(emails(admins)) == sorted([cast.jadmin_et.email, cast.jadmin_iac.email])
    supers = cast.jsuper.get("/api/admin/users", role=SUPER, params={"role": SUPER}).json()
    assert emails(supers) == [cast.jsuper.email]
    in_iac = cast.jsuper.get("/api/admin/users", role=SUPER, params={"jurisdiction": "IAC", "role": ADMIN}).json()
    assert emails(in_iac) == [cast.jadmin_iac.email]


def test_a_system_administrator_lists_everyone_to_find_who_to_promote_but_never_a_system_administrator(world, cast):
    page = cast.sysadmin.get("/api/admin/users").json()
    assert page["total"] == 8
    # They themselves signed in, and have no assignment: nothing marks them as the SystemAdministrator.
    mine = next(i for i in page["items"] if i["email"] == cast.sysadmin.email)
    assert mine["assignments"] == [] and "SystemAdministrator" not in str(page)

    holders = cast.sysadmin.get("/api/admin/users", params={"jurisdiction": "IAC", "role": ADMIN}).json()
    assert emails(holders) == [cast.jadmin_iac.email]


@pytest.mark.parametrize("who", ["judge_iac", "nobody"])
def test_a_judge_or_a_user_with_no_role_cannot_list_users(world, cast, who):
    person = getattr(cast, who)
    response = person.get("/api/admin/users", role=JUDGE if who == "judge_iac" else None, jurisdiction="IAC" if who == "judge_iac" else None)
    assert response.status_code == 403


def test_the_list_is_paged_and_ordered_by_email(world, cast):
    first = cast.jsuper.get("/api/admin/users", role=SUPER, params={"page": 1, "page_size": 3}).json()
    second = cast.jsuper.get("/api/admin/users", role=SUPER, params={"page": 2, "page_size": 3}).json()
    third = cast.jsuper.get("/api/admin/users", role=SUPER, params={"page": 3, "page_size": 3}).json()
    assert [len(p["items"]) for p in (first, second, third)] == [3, 3, 2]
    assert first["total"] == second["total"] == third["total"] == 8
    everyone = emails(first) + emails(second) + emails(third)
    assert everyone == sorted(everyone, key=str.lower) and len(set(everyone)) == 8
    assert cast.jsuper.get("/api/admin/users", role=SUPER, params={"page_size": 201}).status_code == 422
    assert cast.jsuper.get("/api/admin/users", role=SUPER, params={"page": 0}).status_code == 422


def test_people_added_by_email_are_listed_as_pending_after_those_who_have_signed_in(world, cast):
    for email, jurisdiction, role in (
        ("zed@justice.gov.uk", "IAC", JUDGE),
        ("zed@justice.gov.uk", "IAC", LTM),
        ("amy@justice.gov.uk", "ET", JUDGE),
    ):
        assert cast.jsuper.post(
            "/api/admin/assignments", role=SUPER, json={"email": email, "jurisdiction": jurisdiction, "role": role}
        ).status_code == 201

    page = cast.jsuper.get("/api/admin/users", role=SUPER).json()
    assert page["total"] == 10
    pending = [i for i in page["items"] if i["pending"]]
    assert [i["email"] for i in pending] == ["amy@justice.gov.uk", "zed@justice.gov.uk"]
    assert page["items"][-2:] == pending  # after everyone who has signed in
    zed = pending[1]
    assert zed["name"] is None and zed["is_active"] is True
    assert sorted(a["role"] for a in zed["assignments"]) == [JUDGE, LTM]
    # The id of a pending row is its first assignment's: not a user, so it cannot be deactivated.
    assert zed["id"] in {a["id"] for a in zed["assignments"]}
    assert cast.jsuper.post(f"/api/admin/users/{zed['id']}/deactivate", role=SUPER).status_code == 404

    # A page boundary can fall between the two kinds: 8 users then 2 pending, in pages of 3.
    boundary = cast.jsuper.get("/api/admin/users", role=SUPER, params={"page": 3, "page_size": 3}).json()
    assert [i["pending"] for i in boundary["items"]] == [False, False, True]
    tail = cast.jsuper.get("/api/admin/users", role=SUPER, params={"page": 4, "page_size": 3}).json()
    assert [i["email"] for i in tail["items"]] == ["zed@justice.gov.uk"]

    # Filters reach them too: a role, a jurisdiction, a search.
    only_et = cast.jsuper.get("/api/admin/users", role=SUPER, params={"jurisdiction": "ET", "search": "amy"}).json()
    assert emails(only_et) == ["amy@justice.gov.uk"] and only_et["items"][0]["pending"] is True


def test_a_jurisdiction_administrator_sees_only_its_jurisdictions_pending_rows(world, cast):
    for email, jurisdiction in (("here@justice.gov.uk", "IAC"), ("there@justice.gov.uk", "ET")):
        cast.jsuper.post("/api/admin/assignments", role=SUPER, json={"email": email, "jurisdiction": jurisdiction, "role": JUDGE})
    cast.sysadmin.post("/api/admin/assignments", json={"email": "global@justice.gov.uk", "jurisdiction": None, "role": SUPER})

    listed = cast.jadmin_iac.get("/api/admin/users", role=ADMIN, jurisdiction="IAC").json()
    assert [i["email"] for i in listed["items"] if i["pending"]] == ["here@justice.gov.uk"]
    # Not even by searching for them.
    for needle in ("there@", "global@"):
        assert cast.jadmin_iac.get(
            "/api/admin/users", role=ADMIN, jurisdiction="IAC", params={"search": needle}
        ).json()["items"] == []


def test_a_deactivated_user_is_listed_as_inactive(world, cast):
    assert cast.jsuper.post(f"/api/admin/users/{cast.judge_iac.user_id}/deactivate", role=SUPER).status_code == 204
    page = cast.jsuper.get("/api/admin/users", role=SUPER, params={"search": "ivy"}).json()
    assert page["items"][0]["is_active"] is False


# --- deactivating and reactivating ---------------------------------------------------------------------------


def test_a_jurisdiction_administrator_deactivates_and_reactivates_a_user_of_its_jurisdiction(world, cast):
    deactivated = cast.jadmin_iac.post(
        f"/api/admin/users/{cast.judge_iac.user_id}/deactivate", role=ADMIN, jurisdiction="IAC"
    )
    assert deactivated.status_code == 204 and deactivated.content == b""
    assert world.user(cast.judge_iac).is_active is False

    # At once, a token they already hold is refused; and they cannot sign in.
    signed_in = cast.judge_iac.sign_in()
    assert signed_in.status_code == 403 and signed_in.json()["detail"]["code"] == "user_deactivated"

    (row,) = world.audit("user.deactivated")
    assert row.user_id == str(cast.jadmin_iac.user_id) and row.active_role == ADMIN
    assert row.active_jurisdiction_id == world.iac.id
    assert row.detail["target_user_id"] == str(cast.judge_iac.user_id) and row.detail["jurisdictions"] == ["IAC"]

    reactivated = cast.jadmin_iac.post(
        f"/api/admin/users/{cast.judge_iac.user_id}/reactivate", role=ADMIN, jurisdiction="IAC"
    )
    assert reactivated.status_code == 204
    assert world.user(cast.judge_iac).is_active is True
    assert cast.judge_iac.sign_in().status_code == 200
    assert len(world.audit("user.reactivated")) == 1


def test_a_token_minted_before_the_deactivation_is_refused_at_once(world, cast):
    token = cast.judge_iac.headers(JUDGE, "IAC")
    assert world.client.get("/api/user/onboarding-status", headers=token).status_code == 200
    cast.jsuper.post(f"/api/admin/users/{cast.judge_iac.user_id}/deactivate", role=SUPER)
    refused = world.client.get("/api/user/onboarding-status", headers=token)
    assert refused.status_code == 403 and code(refused) == "user_deactivated"


def test_nobody_deactivates_themselves(world, cast):
    for person, role, at in ((cast.jadmin_iac, ADMIN, "IAC"), (cast.jsuper, SUPER, None)):
        refused = person.post(f"/api/admin/users/{person.user_id}/deactivate", role=role, jurisdiction=at)
        assert refused.status_code == 403 and code(refused) == "cannot_deactivate_self"
        assert world.user(person).is_active is True
    assert world.audit("user.deactivated") == []


def test_deactivation_is_idempotent_and_audited_once(world, cast):
    for _ in range(2):
        assert cast.jsuper.post(f"/api/admin/users/{cast.nobody.user_id}/deactivate", role=SUPER).status_code == 204
    assert len(world.audit("user.deactivated")) == 1
    for _ in range(2):
        assert cast.jsuper.post(f"/api/admin/users/{cast.nobody.user_id}/reactivate", role=SUPER).status_code == 204
    assert len(world.audit("user.reactivated")) == 1


def test_a_jurisdiction_administrator_cannot_reach_users_outside_its_jurisdiction(world, cast):
    # Someone who works only in ET is not in the IAC administrator's list: as if they did not exist.
    absent = cast.jadmin_iac.post(f"/api/admin/users/{cast.judge_et.user_id}/deactivate", role=ADMIN, jurisdiction="IAC")
    assert absent.status_code == 404 and code(absent) == "user_not_found"
    assert world.user(cast.judge_et).is_active is True
    # So is someone with no role at all.
    none = cast.jadmin_iac.post(f"/api/admin/users/{cast.nobody.user_id}/deactivate", role=ADMIN, jurisdiction="IAC")
    assert none.status_code == 404
    # Someone who also works elsewhere is switched off everywhere: refused, remove their IAC role instead.
    both = cast.jadmin_iac.post(f"/api/admin/users/{cast.both.user_id}/deactivate", role=ADMIN, jurisdiction="IAC")
    assert both.status_code == 403 and code(both) == "assignment_not_allowed"
    assert world.user(cast.both).is_active is True


def test_whoever_may_not_assign_a_role_may_not_deactivate_its_holder(world, cast):
    other_admin = world.person("Second Admin", ("IAC", ADMIN))
    refused = cast.jadmin_iac.post(f"/api/admin/users/{other_admin.user_id}/deactivate", role=ADMIN, jurisdiction="IAC")
    assert refused.status_code == 403 and code(refused) == "assignment_not_allowed"
    assert world.user(other_admin).is_active is True
    # A super administrator may deactivate a jurisdiction administrator, not a super administrator.
    assert cast.jsuper.post(f"/api/admin/users/{other_admin.user_id}/deactivate", role=SUPER).status_code == 204
    second_super = world.person("Second Super", (None, SUPER))
    refused = cast.jsuper.post(f"/api/admin/users/{second_super.user_id}/deactivate", role=SUPER)
    assert refused.status_code == 403 and code(refused) == "assignment_not_allowed"


def test_a_system_administrator_and_a_judge_cannot_deactivate(world, cast):
    system = cast.sysadmin.post(f"/api/admin/users/{cast.nobody.user_id}/deactivate")
    assert system.status_code == 403
    judge = cast.judge_iac.post(f"/api/admin/users/{cast.nobody.user_id}/deactivate", role=JUDGE, jurisdiction="IAC")
    assert judge.status_code == 403 and code(judge) == "permission_denied"
    assert world.user(cast.nobody).is_active is True


def test_an_unknown_user_is_a_404(world, cast):
    from uuid import uuid4

    assert cast.jsuper.post(f"/api/admin/users/{uuid4()}/deactivate", role=SUPER).status_code == 404
    assert cast.jsuper.post(f"/api/admin/users/{uuid4()}/reactivate", role=SUPER).status_code == 404


# --- the role-change history ----------------------------------------------------------------------------------


def _history(person: Person, *, role, at=None, **params):
    return person.get("/api/admin/role-history", role=role, jurisdiction=at, params=params)


def test_a_change_appears_in_the_history_with_the_contracts_shape(world, cast):
    created = cast.jadmin_iac.post(
        "/api/admin/assignments",
        role=ADMIN,
        jurisdiction="IAC",
        json={"user_id": str(cast.nobody.user_id), "jurisdiction": "IAC", "role": JUDGE},
    ).json()
    assert created["id"]

    page = _history(cast.jadmin_iac, role=ADMIN, at="IAC").json()
    assert set(page) == {"items", "total"} and page["total"] == 1
    (entry,) = page["items"]
    assert set(entry) == {"id", "timestamp", "actor", "actor_context", "target", "change", "jurisdiction", "role"}
    assert entry["actor"] == {"id": str(cast.jadmin_iac.user_id), "email": cast.jadmin_iac.email}
    assert entry["actor_context"] == {"jurisdiction": "IAC", "role": ADMIN}
    assert entry["target"] == {"id": str(cast.nobody.user_id), "email": cast.nobody.email}
    assert entry["change"] == "assignment.added" and entry["jurisdiction"] == "IAC" and entry["role"] == JUDGE
    assert entry["timestamp"].startswith("20")


def test_the_history_is_newest_first_and_paged(world, cast):
    for role in (JUDGE, LTM):
        cast.jadmin_iac.post(
            "/api/admin/assignments",
            role=ADMIN,
            jurisdiction="IAC",
            json={"user_id": str(cast.nobody.user_id), "jurisdiction": "IAC", "role": role},
        )
    cast.jadmin_iac.post(f"/api/admin/users/{cast.judge_iac.user_id}/deactivate", role=ADMIN, jurisdiction="IAC")

    page = _history(cast.jadmin_iac, role=ADMIN, at="IAC").json()
    assert [e["change"] for e in page["items"]] == ["user.deactivated", "assignment.added", "assignment.added"]
    assert [e["role"] for e in page["items"]] == [None, LTM, JUDGE]
    first = _history(cast.jadmin_iac, role=ADMIN, at="IAC", page=1, page_size=2).json()
    second = _history(cast.jadmin_iac, role=ADMIN, at="IAC", page=2, page_size=2).json()
    assert first["total"] == second["total"] == 3 and (len(first["items"]), len(second["items"])) == (2, 1)
    assert second["items"][0]["role"] == JUDGE


def test_a_jurisdiction_administrator_reads_its_own_jurisdictions_history_only(world, cast):
    cast.jsuper.post("/api/admin/assignments", role=SUPER, json={"user_id": str(cast.nobody.user_id), "jurisdiction": "IAC", "role": JUDGE})
    cast.jsuper.post("/api/admin/assignments", role=SUPER, json={"user_id": str(cast.nobody.user_id), "jurisdiction": "ET", "role": JUDGE})
    cast.sysadmin.post("/api/admin/assignments", json={"user_id": str(cast.nobody.user_id), "jurisdiction": None, "role": SUPER})

    mine = _history(cast.jadmin_iac, role=ADMIN, at="IAC").json()
    assert [(e["jurisdiction"], e["role"]) for e in mine["items"]] == [("IAC", JUDGE)]
    # Done by someone else, in a context that is not theirs: it is still their jurisdiction's.
    assert mine["items"][0]["actor_context"] == {"jurisdiction": None, "role": SUPER}

    refused = _history(cast.jadmin_iac, role=ADMIN, at="IAC", jurisdiction="ET")
    assert refused.status_code == 403 and code(refused) == "jurisdiction_not_in_scope"
    own = _history(cast.jadmin_iac, role=ADMIN, at="IAC", jurisdiction="IAC").json()
    assert own["total"] == 1


def test_a_super_administrator_reads_all_of_it_and_can_filter_by_jurisdiction(world, cast):
    cast.jsuper.post("/api/admin/assignments", role=SUPER, json={"user_id": str(cast.nobody.user_id), "jurisdiction": "IAC", "role": JUDGE})
    cast.jadmin_et.post("/api/admin/assignments", role=ADMIN, jurisdiction="ET", json={"user_id": str(cast.nobody.user_id), "jurisdiction": "ET", "role": LTM})
    cast.sysadmin.post("/api/admin/assignments", json={"user_id": str(cast.nobody.user_id), "jurisdiction": None, "role": SUPER})

    everything = _history(cast.jsuper, role=SUPER).json()
    assert everything["total"] == 3
    assert sorted((e["jurisdiction"], e["role"]) for e in everything["items"] if e["jurisdiction"]) == [("ET", LTM), ("IAC", JUDGE)]
    global_change = next(e for e in everything["items"] if e["jurisdiction"] is None)
    assert global_change["role"] == SUPER and global_change["actor_context"] is None
    et = _history(cast.jsuper, role=SUPER, jurisdiction="ET").json()
    assert [(e["jurisdiction"], e["role"]) for e in et["items"]] == [("ET", LTM)]
    assert _history(cast.jsuper, role=SUPER, jurisdiction="NOPE").status_code == 404


def test_a_user_leaving_a_jurisdiction_is_in_its_history(world, cast):
    left = cast.both.delete("/api/me/jurisdictions/ET", role=LTM, jurisdiction="ET")
    assert left.status_code == 200
    (entry,) = _history(cast.jadmin_et, role=ADMIN, at="ET").json()["items"]
    assert entry["change"] == "membership.leave" and entry["jurisdiction"] == "ET" and entry["role"] == LTM
    assert entry["actor"]["email"] == cast.both.email == entry["target"]["email"]
    assert entry["target"]["id"] == str(cast.both.user_id)
    assert _history(cast.jadmin_iac, role=ADMIN, at="IAC").json()["items"] == []


def test_deactivation_of_a_user_who_works_in_several_jurisdictions_is_for_the_super_administrator_alone(world, cast):
    cast.jsuper.post(f"/api/admin/users/{cast.both.user_id}/deactivate", role=SUPER)
    cast.jsuper.post(f"/api/admin/users/{cast.judge_iac.user_id}/deactivate", role=SUPER)
    cast.jsuper.post(f"/api/admin/users/{cast.judge_iac.user_id}/reactivate", role=SUPER)

    everything = _history(cast.jsuper, role=SUPER).json()["items"]
    assert [(e["change"], e["jurisdiction"]) for e in everything] == [
        ("user.reactivated", "IAC"),
        ("user.deactivated", "IAC"),
        ("user.deactivated", None),
    ]
    # IAC's administrator sees the one-jurisdiction user, and not the one who also works in ET.
    mine = _history(cast.jadmin_iac, role=ADMIN, at="IAC").json()["items"]
    assert [e["change"] for e in mine] == ["user.reactivated", "user.deactivated"]


@pytest.mark.parametrize("who", ["judge_iac", "nobody", "sysadmin"])
def test_nobody_else_reads_the_history(world, cast, who):
    person = getattr(cast, who)
    role = JUDGE if who == "judge_iac" else None
    response = _history(person, role=role, at="IAC" if role else None)
    assert response.status_code == 403
