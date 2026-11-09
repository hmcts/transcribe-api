"""Email notifications for access requests through GOV.UK Notify (DIAAT-398), with a fake client.

Who is emailed when a request is made (the active JurisdictionAdministrators of its jurisdiction and
the JurisdictionSuperAdministrators, once each), who when it is decided (the requester; never for a
withdrawal), what the email carries, and that nothing about sending can fail or hold up the request
or the decision.
"""

from __future__ import annotations

import logging

import pytest
from sqlmodel import Session

from tests.unit import auth_fixtures as af
from tests.unit.admin_fixtures import ADMIN, JUDGE, LTM, SUPER, make_world
from transcribe_api.domain import access_requests
from transcribe_api.domain.models_dictation import User
from transcribe_api.domain.notify import access_request_emails, gov_notify

pytestmark = pytest.mark.real_jurisdictions

TEMPLATES = {"access_request": "template-new-request", "access_request_decided": "template-decided"}


class FakeNotify:
    """Stands in for the Notify client: records what would be sent, optionally failing."""

    def __init__(self, fail_for: set[str] | None = None, fail_all: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail_for = fail_for or set()
        self.fail_all = fail_all

    def send_email_notification(self, email_address, template_id, personalisation=None, **kwargs):
        if self.fail_all or email_address in self.fail_for:
            # A Notify error can quote the address: it must not reach the log.
            msg = f"Notify said no to {email_address}"
            raise RuntimeError(msg)
        self.sent.append({"to": email_address, "template": template_id, "personalisation": personalisation})


@pytest.fixture
def notify(monkeypatch):
    fake = FakeNotify()
    monkeypatch.setattr(gov_notify, "_get_notifications_client", lambda: fake)
    monkeypatch.setattr(access_request_emails, "_template_id", lambda kind: TEMPLATES[kind])
    return fake


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
            "admin_iac": world.person("Ada Admin", ("IAC", ADMIN), email="ada@justice.gov.uk"),
            "admin_iac_2": world.person("Abe Admin", ("IAC", ADMIN), email="abe@justice.gov.uk"),
            "admin_et": world.person("Eli Admin", ("ET", ADMIN), email="eli@justice.gov.uk"),
            "super_1": world.person("Sue Super", (None, SUPER), email="sue@justice.gov.uk"),
            "super_2": world.person("Sam Super", (None, SUPER), email="sam@justice.gov.uk"),
            "judge": world.person("Ivy Judge", ("IAC", JUDGE), email="ivy@justice.gov.uk"),
            "requester": world.person("Rita Requester", email="rita@justice.gov.uk"),
        },
    )


def ask(person, where="IAC", roles=(JUDGE,)):
    return person.post("/api/access-requests", json={"jurisdiction": where, "roles": list(roles)})


def recipients(fake: FakeNotify, template: str) -> list[str]:
    return sorted(m["to"] for m in fake.sent if m["template"] == template)


# --- a new request: the approvers ---------------------------------------------------------------------------


def test_a_new_request_emails_the_jurisdictions_administrators_and_every_super_administrator_once_each(world, cast, notify):
    response = ask(cast.requester, roles=(JUDGE, LTM))

    assert response.status_code == 201
    assert recipients(notify, "template-new-request") == [
        "abe@justice.gov.uk",
        "ada@justice.gov.uk",
        "sam@justice.gov.uk",
        "sue@justice.gov.uk",
    ]
    assert len(notify.sent) == 4  # and nothing to the ET's administrator, the judge or the requester


def test_the_email_carries_the_requesters_name_the_jurisdiction_the_roles_and_a_link_and_nothing_else(world, cast, notify):
    request_id = ask(cast.requester, roles=(LTM, JUDGE)).json()["id"]

    (mail, *_rest) = [m for m in notify.sent if m["to"] == "ada@justice.gov.uk"]
    assert mail["personalisation"] == {
        "requester_name": "Rita Requester",
        "jurisdiction_name": "Immigration and Asylum Chamber",
        "roles": "Judge, LegalTextManager",
        "link": f"http://localhost:8000/admin?tab=approvals&request={request_id}",
    }
    # The link carries only the request's ID: no token, no address.
    link = mail["personalisation"]["link"]
    for secret in ("token", "Bearer", "@", "key", "sig="):
        assert secret not in link, secret


def test_a_user_who_is_both_an_administrator_and_a_super_administrator_is_emailed_once(world, cast, notify):
    world.give(cast.admin_iac, None, SUPER)
    ask(cast.requester)
    assert recipients(notify, "template-new-request").count("ada@justice.gov.uk") == 1
    assert len(notify.sent) == 4


def test_two_user_rows_with_one_address_get_one_email(world, cast, notify):
    twin = world.person("Ada Again", ("IAC", ADMIN), email="ADA@justice.gov.uk")
    assert twin.user_id != cast.admin_iac.user_id
    ask(cast.requester)
    assert [a.lower() for a in recipients(notify, "template-new-request")].count("ada@justice.gov.uk") == 1


def test_a_deactivated_administrator_is_not_emailed(world, cast, notify):
    assert cast.super_1.post(f"/api/admin/users/{cast.admin_iac_2.user_id}/deactivate", role=SUPER).status_code == 204
    with Session(world.engine) as session:  # a super administrator is switched off by nobody in the app
        other = session.get(User, cast.super_2.user_id)
        other.is_active = False
        session.add(other)
        session.commit()
    ask(cast.requester)
    assert recipients(notify, "template-new-request") == ["ada@justice.gov.uk", "sue@justice.gov.uk"]


def test_the_requester_is_not_emailed_about_their_own_request_even_as_an_administrator(world, cast, notify):
    ask(cast.admin_iac, where="IAC", roles=(LTM,))  # the JurisdictionAdministrator asks for another role
    assert "ada@justice.gov.uk" not in recipients(notify, "template-new-request")
    assert "abe@justice.gov.uk" in recipients(notify, "template-new-request")


def test_a_request_for_the_et_goes_to_the_ets_administrators(world, cast, notify):
    ask(cast.requester, where="ET")
    assert recipients(notify, "template-new-request") == ["eli@justice.gov.uk", "sam@justice.gov.uk", "sue@justice.gov.uk"]


def test_a_refused_request_emails_nobody(world, cast, notify):
    assert ask(cast.requester, roles=(ADMIN,)).status_code == 400
    assert ask(cast.requester, where="NOPE").status_code == 404
    assert ask(cast.requester).status_code == 201
    notify.sent.clear()
    assert ask(cast.requester).status_code == 409  # a duplicate
    assert notify.sent == []


def test_the_domain_function_sends_nothing_the_route_does_it_after_the_response(world, cast, notify):
    with Session(world.engine) as session:
        user = session.get(User, cast.requester.user_id)
        created = access_requests.create_request(session, user, jurisdiction_code="IAC", roles=[JUDGE])
    assert notify.sent == []
    access_request_emails.notify_approvers(created.id)
    assert len(notify.sent) == 4


# --- a decision: the requester ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("decision", "outcome"), [("approve", "approved"), ("reject", "rejected")])
def test_a_decision_emails_the_requester_once_with_the_outcome(world, cast, notify, decision, outcome):
    request_id = ask(cast.requester, roles=(JUDGE, LTM)).json()["id"]
    notify.sent.clear()

    response = cast.admin_iac.post(
        f"/api/admin/access-requests/{request_id}/{decision}",
        role=ADMIN,
        jurisdiction="IAC",
        json={"reason": "a private reason the requester reads in the app"},
    )

    assert response.status_code == 200
    (mail,) = notify.sent
    assert mail["to"] == "rita@justice.gov.uk" and mail["template"] == "template-decided"
    assert mail["personalisation"] == {
        "jurisdiction_name": "Immigration and Asylum Chamber",
        "roles": "Judge, LegalTextManager",
        "outcome": outcome,
        "link": "http://localhost:8000/",
    }
    assert "private reason" not in str(mail)


def test_a_withdrawal_sends_no_email_to_anyone(world, cast, notify):
    request_id = ask(cast.requester).json()["id"]
    notify.sent.clear()
    assert cast.requester.post(f"/api/access-requests/{request_id}/withdraw").status_code == 200
    assert notify.sent == []


def test_a_refused_decision_sends_no_email(world, cast, notify):
    request_id = ask(cast.requester).json()["id"]
    notify.sent.clear()
    # Another jurisdiction's administrator, and a request already decided.
    assert cast.admin_et.post(f"/api/admin/access-requests/{request_id}/approve", role=ADMIN, jurisdiction="ET").status_code == 403
    assert cast.admin_iac.post(f"/api/admin/access-requests/{request_id}/approve", role=ADMIN, jurisdiction="IAC").status_code == 200
    notify.sent.clear()
    assert cast.admin_iac.post(f"/api/admin/access-requests/{request_id}/reject", role=ADMIN, jurisdiction="IAC").status_code == 409
    assert notify.sent == []


# --- sending never fails the request or the decision -----------------------------------------------------------------


def test_if_notify_is_down_the_request_and_the_decision_are_still_saved(world, cast, monkeypatch, caplog):
    down = FakeNotify(fail_all=True)
    monkeypatch.setattr(gov_notify, "_get_notifications_client", lambda: down)
    monkeypatch.setattr(access_request_emails, "_template_id", lambda kind: TEMPLATES[kind])

    with caplog.at_level(logging.INFO):
        created = ask(cast.requester)
        assert created.status_code == 201
        decided = cast.admin_iac.post(f"/api/admin/access-requests/{created.json()['id']}/approve", role=ADMIN, jurisdiction="IAC")
        assert decided.status_code == 200

    assert world.assignments_of(cast.requester) == {("IAC", JUDGE)}
    assert cast.requester.get("/api/access-requests/mine").json()[0]["status"] == "approved"
    log = caplog.text
    assert "failed (RuntimeError)" in log
    # No address, and not the error text that quotes one.
    assert "@" not in log and "Notify said no" not in log


def test_one_recipient_failing_does_not_stop_the_others(world, cast, monkeypatch):
    flaky = FakeNotify(fail_for={"ada@justice.gov.uk"})
    monkeypatch.setattr(gov_notify, "_get_notifications_client", lambda: flaky)
    monkeypatch.setattr(access_request_emails, "_template_id", lambda kind: TEMPLATES[kind])

    assert ask(cast.requester).status_code == 201

    assert recipients(flaky, "template-new-request") == ["abe@justice.gov.uk", "sam@justice.gov.uk", "sue@justice.gov.uk"]


def test_a_failure_to_even_build_the_client_is_survived(world, cast, monkeypatch):
    def broken():
        msg = "no client"
        raise RuntimeError(msg)

    monkeypatch.setattr(gov_notify, "_get_notifications_client", broken)
    assert ask(cast.requester).status_code == 201
    assert cast.requester.get("/api/access-requests/mine").json()[0]["status"] == "pending"


def test_a_failure_while_working_out_who_to_email_is_survived(world, cast, notify, monkeypatch):
    def broken(*args, **kwargs):
        msg = "database down"
        raise RuntimeError(msg)

    monkeypatch.setattr(access_request_emails, "approver_recipients", broken)
    assert ask(cast.requester).status_code == 201
    assert notify.sent == []


# --- no key, no template ------------------------------------------------------------------------------------------------


def test_with_no_notify_key_nothing_is_sent_and_the_log_says_what_would_have_been(world, cast, monkeypatch, caplog):
    monkeypatch.setattr(gov_notify, "_get_notifications_client", lambda: None)
    monkeypatch.setattr(access_request_emails, "_template_id", lambda kind: TEMPLATES[kind])

    with caplog.at_level(logging.INFO):
        request_id = ask(cast.requester).json()["id"]
        cast.admin_iac.post(f"/api/admin/access-requests/{request_id}/reject", role=ADMIN, jurisdiction="IAC")

    assert "would send email=access_request template=template-new-request to 4 recipient(s)" in caplog.text
    assert "would send email=access_request_decided template=template-decided to 1 recipient(s)" in caplog.text
    # The number of recipients, never an address (and no request content).
    assert "@" not in caplog.text.replace("localhost.test", "")
    assert "Rita" not in caplog.text


def test_with_a_key_but_no_template_nothing_is_sent_and_the_log_says_why(world, cast, monkeypatch, caplog):
    fake = FakeNotify()
    monkeypatch.setattr(gov_notify, "_get_notifications_client", lambda: fake)
    monkeypatch.setattr(access_request_emails, "_template_id", lambda kind: None)

    with caplog.at_level(logging.INFO):
        assert ask(cast.requester).status_code == 201

    assert fake.sent == []
    assert "template id is not set" in caplog.text


def test_the_notify_template_ids_are_settings_and_blank_by_default():
    from transcribe_api.runtime.settings_dictation import Settings

    fields = Settings.model_fields
    assert fields["GOV_NOTIFY_TEMPLATE_ACCESS_REQUEST"].default is None
    assert fields["GOV_NOTIFY_TEMPLATE_ACCESS_DECIDED"].default is None


def test_the_template_ids_come_from_the_settings(monkeypatch):
    from transcribe_api.runtime import settings_dictation

    monkeypatch.setenv("GOV_NOTIFY_TEMPLATE_ACCESS_REQUEST", "req-id")
    monkeypatch.setenv("GOV_NOTIFY_TEMPLATE_ACCESS_DECIDED", "dec-id")
    settings_dictation.get_settings.cache_clear()
    try:
        assert access_request_emails._template_id("access_request") == "req-id"
        assert access_request_emails._template_id("access_request_decided") == "dec-id"
    finally:
        settings_dictation.get_settings.cache_clear()
