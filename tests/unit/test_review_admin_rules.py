"""The administration rules the independent reviews tightened (findings S3, S4, S-low a/b/e, D4, integration).

Real API, real tokens (tests/unit/admin_fixtures.py). Beside ``test_admin_assignments.py``,
``test_admin_users.py`` and ``test_access_requests.py``, which hold the rules they did not change.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from tests.unit import auth_fixtures as af
from tests.unit.admin_fixtures import ADMIN, JUDGE, LTM, SUPER, make_world
from transcribe_api.domain import user_admin
from transcribe_api.domain.assignments import add_assignment
from transcribe_api.domain.auth.app_token import Context
from transcribe_api.domain.models_dictation import Jurisdiction

pytestmark = pytest.mark.real_jurisdictions


@pytest.fixture(autouse=True)
def no_email(monkeypatch):
    from transcribe_api.domain.notify import gov_notify

    monkeypatch.setattr(gov_notify, "_get_notifications_client", lambda: None)


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
            "jsuper_2": world.person("Super Two", (None, SUPER)),
            "sysadmin": world.person("Sysadmin", sysadmin=True),
            "judge_iac": world.person("Ivy Judge", ("IAC", JUDGE)),
            "judge_et": world.person("Eve Judge", ("ET", JUDGE)),
            "nobody": world.person("Nora Nobody"),
        },
    )


def code(response) -> str:
    return response.json()["detail"]["code"]


def assign_body(**kw):
    return {"user_id": None, "email": None, "jurisdiction": "IAC", "role": JUDGE} | kw


# --- S3: an email that matches more than one user row is never guessed ----------------------------------------


def test_assigning_by_an_email_that_two_users_signed_in_with_is_refused_not_given_to_the_newest(world, cast):
    shared = "twin@justice.gov.uk"
    first = world.person("Twin One", email=shared)
    second = world.person("Twin Two", email=shared.upper())  # another Entra account, the same address

    refused = cast.jadmin_iac.post(
        "/api/admin/assignments", role=ADMIN, jurisdiction="IAC", json=assign_body(email=shared)
    )

    assert refused.status_code == 409 and code(refused) == "ambiguous_email"
    assert world.assignments_of(first) == set() == world.assignments_of(second)
    assert world.pending_for(shared) == set()  # and not left pending either
    # By user id there is no doubt: the administrator picks the person from the list.
    chosen = cast.jadmin_iac.post(
        "/api/admin/assignments", role=ADMIN, jurisdiction="IAC", json=assign_body(user_id=str(first.user_id))
    )
    assert chosen.status_code == 201 and world.assignments_of(first) == {("IAC", JUDGE)}


def test_one_user_with_that_email_is_still_assigned_by_email(world, cast):
    only = world.person("Only One", email="only@justice.gov.uk")
    done = cast.jadmin_iac.post(
        "/api/admin/assignments", role=ADMIN, jurisdiction="IAC", json=assign_body(email="Only@Justice.gov.uk")
    )
    assert done.status_code == 201 and world.assignments_of(only) == {("IAC", JUDGE)}


def test_a_pending_claim_when_another_user_has_the_address_is_made_first_match_and_logged_without_the_address(
    world, cast, caplog
):
    email = "claimme@justice.gov.uk"
    world.person("Earlier", email=email)  # an account that already has the address
    with Session(world.engine) as session:
        add_assignment(session, role=JUDGE, jurisdiction_id=world.iac.id, email=email)  # a leftover pending row
        session.commit()

    with caplog.at_level(logging.WARNING, logger="transcribe_api.domain.assignments"):
        later = world.person("Later", email=email)

    assert world.assignments_of(later) == {("IAC", JUDGE)}  # first match: behaviour unchanged
    warnings = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "same email" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert email not in warnings[0] and "@" not in warnings[0]


def test_a_pending_claim_with_no_other_user_does_not_warn(world, caplog):
    email = "quiet@justice.gov.uk"
    with Session(world.engine) as session:
        add_assignment(session, role=JUDGE, jurisdiction_id=world.iac.id, email=email)
        session.commit()
    with caplog.at_level(logging.WARNING, logger="transcribe_api.domain.assignments"):
        world.person("Quiet", email=email)
    assert not [r for r in caplog.records if "same email" in r.getMessage()]


# --- S4: a SystemAdministrator acting in a stored role has that role's scope, not a global one ----------------------


@pytest.fixture
def sysadmin_as_jadmin(world, cast):
    """The SystemAdministrator who was given JurisdictionAdministrator in IAC (by a JurisdictionSuperAdministrator) and acts in it."""
    world.give(cast.sysadmin, "IAC", ADMIN)
    return cast.sysadmin


def test_a_system_administrator_acting_as_a_jurisdiction_administrator_cannot_deactivate_a_super_administrator(
    world, cast, sysadmin_as_jadmin
):
    response = sysadmin_as_jadmin.post(
        f"/api/admin/users/{cast.jsuper.user_id}/deactivate", role=ADMIN, jurisdiction="IAC"
    )

    assert response.status_code in (403, 404), response.text
    assert world.user(cast.jsuper).is_active is True


def test_a_system_administrator_acting_as_a_jurisdiction_administrator_cannot_deactivate_users_of_other_jurisdictions(
    world, cast, sysadmin_as_jadmin
):
    response = sysadmin_as_jadmin.post(
        f"/api/admin/users/{cast.judge_et.user_id}/deactivate", role=ADMIN, jurisdiction="IAC"
    )

    assert response.status_code == 404
    assert world.user(cast.judge_et).is_active is True
    # Inside the jurisdiction the role acts in it can, as any JurisdictionAdministrator.
    assert (
        sysadmin_as_jadmin.post(
            f"/api/admin/users/{cast.judge_iac.user_id}/deactivate", role=ADMIN, jurisdiction="IAC"
        ).status_code
        == 204
    )


def test_a_system_administrator_acting_as_a_jurisdiction_administrator_lists_only_its_jurisdictions_users(
    world, cast, sysadmin_as_jadmin
):
    page = sysadmin_as_jadmin.get("/api/admin/users", role=ADMIN, jurisdiction="IAC").json()
    emails = {item["email"] for item in page["items"]}
    assert cast.judge_iac.email in emails
    assert cast.judge_et.email not in emails and cast.jsuper.email not in emails and cast.jadmin_et.email not in emails

    outside = sysadmin_as_jadmin.get("/api/admin/users", role=ADMIN, jurisdiction="IAC", params={"jurisdiction": "ET"})
    assert outside.status_code == 403 and code(outside) == "jurisdiction_not_in_scope"


def test_a_system_administrator_acting_as_a_super_administrator_deactivates_another_one(world, cast):
    assert (
        cast.sysadmin.post(
            "/api/admin/assignments",
            json=assign_body(user_id=str(cast.sysadmin.user_id), jurisdiction=None, role=SUPER),
        ).status_code
        == 201
    )

    response = cast.sysadmin.post(f"/api/admin/users/{cast.jsuper.user_id}/deactivate", role=SUPER)

    assert response.status_code == 204
    assert world.user(cast.jsuper).is_active is False


def test_a_system_administrator_with_no_context_still_finds_who_to_make_an_administrator(world, cast):
    """Its own permission (jurisdiction_super_admin.assign) reaches the user list: unchanged, for the
    Super administrators screen (it lists, it cannot deactivate: that needs users.manage or accounts.manage)."""
    page = cast.sysadmin.get("/api/admin/users").json()
    emails = {item["email"] for item in page["items"]}
    assert {cast.judge_iac.email, cast.judge_et.email, cast.jsuper.email} <= emails
    assert cast.sysadmin.post(f"/api/admin/users/{cast.judge_iac.user_id}/deactivate").status_code == 403


# --- S-low (a): nobody decides their own access request; a SystemAdministrator may still self-assign ------------------


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_an_administrator_cannot_decide_their_own_request(world, cast, decision):
    asked = cast.jadmin_iac.post("/api/access-requests", json={"jurisdiction": "IAC", "roles": [LTM]})
    assert asked.status_code == 201
    request_id = asked.json()["id"]

    own = cast.jadmin_iac.post(
        f"/api/admin/access-requests/{request_id}/{decision}", role=ADMIN, jurisdiction="IAC", json={}
    )

    assert own.status_code == 403 and code(own) == "own_request"
    assert world.assignments_of(cast.jadmin_iac) == {("IAC", ADMIN)}
    assert cast.jadmin_iac.get("/api/access-requests/mine").json()[0]["status"] == "pending"
    # Another administrator of the jurisdiction decides it.
    other_admin = world.person("Admin IAC Two", ("IAC", ADMIN))
    assert (
        other_admin.post(
            f"/api/admin/access-requests/{request_id}/approve", role=ADMIN, jurisdiction="IAC", json={}
        ).status_code
        == 200
    )
    assert world.assignments_of(cast.jadmin_iac) == {("IAC", ADMIN), ("IAC", LTM)}


def test_a_super_administrator_may_ask_but_decides_no_request_not_even_its_own(world, cast):
    request_id = cast.jsuper.post("/api/access-requests", json={"jurisdiction": "ET", "roles": [JUDGE]}).json()["id"]
    own = cast.jsuper.post(f"/api/admin/access-requests/{request_id}/approve", role=SUPER, json={})
    assert own.status_code == 403 and code(own) == "permission_denied"
    assert world.assignments_of(cast.jsuper) == {(None, SUPER)}
    assert (
        cast.jadmin_et.post(
            f"/api/admin/access-requests/{request_id}/approve", role=ADMIN, jurisdiction="ET", json={}
        ).status_code
        == 200
    )
    assert world.assignments_of(cast.jsuper) == {(None, SUPER), ("ET", JUDGE)}


def test_a_system_administrator_still_gives_themselves_the_global_role_and_takes_it_away(world, cast):
    done = cast.sysadmin.post(
        "/api/admin/assignments", json=assign_body(user_id=str(cast.sysadmin.user_id), jurisdiction=None, role=SUPER)
    )
    assert done.status_code == 201
    assert world.assignments_of(cast.sysadmin) == {(None, SUPER)}
    assert cast.sysadmin.delete(f"/api/admin/assignments/{done.json()['id']}").status_code == 204
    assert world.assignments_of(cast.sysadmin) == set()


# --- S-low (b): at most five requests waiting per user ---------------------------------------------------------------


def test_a_user_has_at_most_five_requests_pending_and_a_withdrawal_makes_room(world, cast):
    asks = [("IAC", [JUDGE]), ("IAC", [LTM]), ("IAC", [JUDGE, LTM]), ("ET", [JUDGE]), ("ET", [LTM])]
    ids = []
    for where, roles in asks:
        made = cast.nobody.post("/api/access-requests", json={"jurisdiction": where, "roles": roles})
        assert made.status_code == 201, made.text
        ids.append(made.json()["id"])

    sixth = cast.nobody.post("/api/access-requests", json={"jurisdiction": "ET", "roles": [JUDGE, LTM]})

    assert sixth.status_code == 409 and code(sixth) == "too_many_pending_requests"
    assert len(cast.nobody.get("/api/access-requests/mine").json()) == 5
    # An identical one is still the duplicate, not the cap.
    assert (
        code(cast.nobody.post("/api/access-requests", json={"jurisdiction": "IAC", "roles": [JUDGE]}))
        == "duplicate_request"
    )
    assert cast.nobody.post(f"/api/access-requests/{ids[0]}/withdraw").status_code == 200
    assert (
        cast.nobody.post("/api/access-requests", json={"jurisdiction": "ET", "roles": [JUDGE, LTM]}).status_code == 201
    )


def test_the_cap_counts_pending_requests_only_and_only_the_requesters_own(world, cast):
    approvers = {"IAC": cast.jadmin_iac, "ET": cast.jadmin_et}
    for where, roles in [("IAC", [JUDGE]), ("IAC", [LTM]), ("IAC", [JUDGE, LTM]), ("ET", [JUDGE]), ("ET", [LTM])]:
        request_id = cast.nobody.post("/api/access-requests", json={"jurisdiction": where, "roles": roles}).json()["id"]
        # decided ones do not count
        rejected = approvers[where].post(
            f"/api/admin/access-requests/{request_id}/reject", role=ADMIN, jurisdiction=where, json={}
        )
        assert rejected.status_code == 200
    assert (
        cast.nobody.post("/api/access-requests", json={"jurisdiction": "ET", "roles": [JUDGE, LTM]}).status_code == 201
    )
    other = world.person("Other Nobody")
    assert other.post("/api/access-requests", json={"jurisdiction": "ET", "roles": [JUDGE, LTM]}).status_code == 201


# --- the cap on waiting requests is no limit on emails: ask, withdraw, ask again --------------------------------------


def test_asking_and_withdrawing_over_and_over_is_stopped_after_ten_requests_a_day(world, cast):
    sent = []
    from transcribe_api.domain import access_requests

    for _ in range(access_requests.MAX_REQUESTS_PER_DAY):
        made = cast.nobody.post("/api/access-requests", json={"jurisdiction": "IAC", "roles": [JUDGE]})
        assert made.status_code == 201, made.text
        sent.append(made.json()["id"])
        assert cast.nobody.post(f"/api/access-requests/{made.json()['id']}/withdraw").status_code == 200

    eleventh = cast.nobody.post("/api/access-requests", json={"jurisdiction": "IAC", "roles": [JUDGE]})

    assert eleventh.status_code == 429 and code(eleventh) == "too_many_requests"
    assert len(cast.nobody.get("/api/access-requests/mine").json()) == access_requests.MAX_REQUESTS_PER_DAY
    # Somebody else is not held to it.
    other = world.person("Other Nobody")
    assert other.post("/api/access-requests", json={"jurisdiction": "IAC", "roles": [JUDGE]}).status_code == 201


def test_requests_older_than_a_day_do_not_count_towards_the_daily_cap(world, cast):
    from datetime import UTC, datetime, timedelta

    from transcribe_api.domain import access_requests
    from transcribe_api.domain.models_dictation import AccessRequest

    for _ in range(access_requests.MAX_REQUESTS_PER_DAY):
        made = cast.nobody.post("/api/access-requests", json={"jurisdiction": "IAC", "roles": [JUDGE]})
        assert cast.nobody.post(f"/api/access-requests/{made.json()['id']}/withdraw").status_code == 200
    with Session(world.engine) as session:  # yesterday's
        for row in session.exec(select(AccessRequest)).all():
            row.created_at = datetime.now(UTC) - timedelta(hours=25)
            session.add(row)
        session.commit()

    assert cast.nobody.post("/api/access-requests", json={"jurisdiction": "IAC", "roles": [JUDGE]}).status_code == 201


# --- integration: nothing is assigned into a disabled jurisdiction ---------------------------------------------------


def _disable(world, code_: str) -> None:
    with Session(world.engine) as session:
        row = session.exec(select(Jurisdiction).where(Jurisdiction.code == code_)).one()
        row.enabled = False
        session.add(row)
        session.commit()


@pytest.mark.parametrize("by", ["user", "email"])
def test_an_assignment_into_a_disabled_jurisdiction_is_409(world, cast, by):
    token = cast.jadmin_et.headers(ADMIN, "ET")  # minted while ET was enabled
    _disable(world, "ET")
    body = (
        assign_body(user_id=str(cast.nobody.user_id), jurisdiction="ET")
        if by == "user"
        else assign_body(email="new@justice.gov.uk", jurisdiction="ET")
    )

    refused = world.client.post("/api/admin/assignments", headers=token, json=body)

    assert refused.status_code == 409 and code(refused) == "jurisdiction_disabled"
    assert world.assignments_of(cast.nobody) == set() and world.pending_for("new@justice.gov.uk") == set()


def test_a_super_administrator_cannot_assign_an_administrator_into_a_disabled_jurisdiction_either(world, cast):
    _disable(world, "ET")
    refused = cast.jsuper.post(
        "/api/admin/assignments",
        role=SUPER,
        json=assign_body(user_id=str(cast.jadmin_iac.user_id), jurisdiction="ET", role=ADMIN),
    )
    assert refused.status_code == 409 and code(refused) == "jurisdiction_disabled"


def test_a_request_for_a_jurisdiction_disabled_since_cannot_be_approved_but_can_be_rejected(world, cast):
    first = cast.nobody.post("/api/access-requests", json={"jurisdiction": "ET", "roles": [JUDGE]}).json()["id"]
    second = cast.nobody.post("/api/access-requests", json={"jurisdiction": "ET", "roles": [LTM]}).json()["id"]
    token = cast.jadmin_et.headers(ADMIN, "ET")  # minted while ET was enabled
    _disable(world, "ET")

    refused = world.client.post(f"/api/admin/access-requests/{first}/approve", headers=token, json={})

    assert refused.status_code == 409 and code(refused) == "jurisdiction_disabled"
    assert world.assignments_of(cast.nobody) == set()
    assert cast.nobody.get("/api/access-requests/mine").json()[-1]["status"] == "pending"
    assert world.client.post(f"/api/admin/access-requests/{second}/reject", headers=token, json={}).status_code == 200


# --- integration: X-Requested-Context matches the role without regard to case -----------------------------------------


@pytest.mark.parametrize("header", ["iac:judge", "IAC:JUDGE", "Iac:Judge", "iac:administrator"])
def test_the_requested_context_is_matched_without_regard_to_case(world, header):
    both = world.person("Both", ("IAC", JUDGE), ("ET", JUDGE))
    response = world.client.post(
        "/api/auth/session",
        headers={"Authorization": f"Bearer {both._entra()}", "X-Requested-Context": header},
    )
    assert response.status_code == 200
    if header.endswith("administrator"):
        assert response.json()["me"]["active"] is None  # not a role they hold (and not a role name): ignored
    else:
        assert response.json()["me"]["active"] == {
            "jurisdiction": {"code": "IAC", "name": "Immigration and Asylum Chamber", "stage": "live"},
            "role": JUDGE,
        }


def test_a_global_context_is_matched_without_regard_to_case_too(world):
    boss = world.person("Boss", (None, SUPER), ("IAC", JUDGE))
    response = world.client.post(
        "/api/auth/session",
        headers={"Authorization": f"Bearer {boss._entra()}", "X-Requested-Context": "-:jurisdictionsuperadministrator"},
    )
    assert response.json()["me"]["active"] == {"jurisdiction": None, "role": SUPER}


# --- S-low (e): the role history does not name a SystemAdministrator -----------------------------------------------------


def test_the_role_history_does_not_name_the_system_administrator_who_made_a_change(world, cast):
    made = cast.sysadmin.post(
        "/api/admin/assignments", json=assign_body(user_id=str(cast.nobody.user_id), jurisdiction=None, role=SUPER)
    )
    assert made.status_code == 201  # acting as SystemAdministrator, assigns the global role
    assert (
        cast.jsuper.post(
            "/api/admin/assignments", role=SUPER, json=assign_body(user_id=str(cast.judge_iac.user_id), role=ADMIN)
        ).status_code
        == 201
    )
    assert (
        cast.jadmin_iac.post(
            "/api/admin/assignments",
            role=ADMIN,
            jurisdiction="IAC",
            json=assign_body(user_id=str(cast.judge_et.user_id), role=LTM),
        ).status_code
        == 201
    )

    # No reader of the history through the API sees global changes (role_audit.read is a
    # JurisdictionAdministrator's, in its jurisdiction), so the global row is read through the domain.
    with Session(world.engine) as session:
        global_reader = SimpleNamespace(id=cast.jsuper.user_id, ctx=Context(None, SUPER), sysadmin=False)
        everything = user_admin.role_history(session, global_reader, jurisdiction=None, page=1, page_size=50).items
    system = next(item for item in everything if item.role == SUPER and item.jurisdiction is None)
    assert (system.actor_id, system.actor_email) == (None, "system administrator")
    assert system.actor_role is None and system.actor_jurisdiction is None
    assert cast.sysadmin.email not in str(everything) and str(cast.sysadmin.user_id) not in str(everything)

    history = cast.jadmin_iac.get("/api/admin/role-history", role=ADMIN, jurisdiction="IAC").json()["items"]
    assert cast.sysadmin.email not in str(history) and str(cast.sysadmin.user_id) not in str(history)
    # An administrator's own changes are still attributed.
    by_role = {item["role"]: item for item in history}
    assert by_role[ADMIN]["actor"] == {"id": str(cast.jsuper.user_id), "email": cast.jsuper.email}
    assert by_role[ADMIN]["actor_context"] == {"jurisdiction": None, "role": SUPER}
    assert by_role[LTM]["actor"] == {"id": str(cast.jadmin_iac.user_id), "email": cast.jadmin_iac.email}
    assert by_role[LTM]["actor_context"] == {"jurisdiction": "IAC", "role": ADMIN}


def test_a_system_administrator_acting_in_a_role_has_only_that_roles_rights_and_is_named_as_its_holder(
    world, cast, sysadmin_as_jadmin
):
    """DIAAT-403: acting as a JurisdictionAdministrator, assigning the JurisdictionAdministrator role (not a
    JurisdictionAdministrator's right) is refused; assigning a Judge is the role's own right, shown as the role's holder."""
    refused = sysadmin_as_jadmin.post(
        "/api/admin/assignments",
        role=ADMIN,
        jurisdiction="IAC",
        json=assign_body(user_id=str(cast.nobody.user_id), role=ADMIN),
    )
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "assignment_not_allowed"
    assert (
        sysadmin_as_jadmin.post(
            "/api/admin/assignments",
            role=ADMIN,
            jurisdiction="IAC",
            json=assign_body(user_id=str(cast.nobody.user_id), role=JUDGE),
        ).status_code
        == 201
    )

    history = cast.jadmin_iac.get("/api/admin/role-history", role=ADMIN, jurisdiction="IAC").json()["items"]
    by_role = {item["role"]: item for item in history if item["target"]["id"] == str(cast.nobody.user_id)}
    assert ADMIN not in by_role
    assert by_role[JUDGE]["actor"]["id"] == str(cast.sysadmin.user_id)  # the role's own right: its holder


# --- D4: a JurisdictionAdministrator's list counts only what is in its scope ------------------------------------------------


def test_the_total_of_a_jurisdiction_administrators_list_counts_only_its_jurisdictions_rows(world, cast):
    for email, where in (
        ("pend-et-1@justice.gov.uk", "ET"),
        ("pend-et-2@justice.gov.uk", "ET"),
        ("pend-iac@justice.gov.uk", "IAC"),
    ):
        admin = cast.jadmin_et if where == "ET" else cast.jadmin_iac
        done = admin.post(
            "/api/admin/assignments", role=ADMIN, jurisdiction=where, json=assign_body(email=email, jurisdiction=where)
        )
        assert done.status_code == 201

    page = cast.jadmin_iac.get("/api/admin/users", role=ADMIN, jurisdiction="IAC", params={"search": "pend"}).json()

    assert [item["email"] for item in page["items"]] == ["pend-iac@justice.gov.uk"]
    assert page["total"] == 1
    # And without a search: the total is the jurisdiction's users, and its one pending row.
    everything = cast.jadmin_iac.get("/api/admin/users", role=ADMIN, jurisdiction="IAC").json()
    assert everything["total"] == len(everything["items"]) == 3  # itself, Ivy Judge, the pending row
    # Paging through it with a page of one never meets another jurisdiction's pending rows.
    seen = []
    for number in (1, 2, 3, 4):
        paged = cast.jadmin_iac.get(
            "/api/admin/users", role=ADMIN, jurisdiction="IAC", params={"page": number, "page_size": 1}
        ).json()
        assert paged["total"] == 3
        seen += [item["email"] for item in paged["items"]]
    assert len(seen) == 3 and not any("pend-et" in email for email in seen)
