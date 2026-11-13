"""Emails about access requests, through GOV.UK Notify (DIAAT-398).

* **New access request**: to every active JurisdictionAdministrator of the request's jurisdiction,
  who alone decide it (DIAAT-406), once each (distinct users and addresses; not the requester, who
  knows). Personalisation: ``requester_name``, ``jurisdiction_name``, ``roles`` and
  ``link``, which opens the request in Admin's approvals queue (``{APP_URL}/admin?tab=approvals&
  request=<id>``): the link carries only the request's ID, the approver signs in and is checked for
  ``access.approve`` like for any other request.
* **Access request decided**: to the requester when it is approved or rejected (never for one they
  withdrew). Personalisation: ``jurisdiction_name``, ``roles``, ``outcome`` (``approved`` or
  ``rejected``) and ``link`` to the app. Nothing of the approver's reason.

Sending never blocks or fails what caused it: the routes hand these functions to FastAPI's background
tasks (after the response), and they catch everything. A failure is logged with the exception's type
only (a Notify error can quote the address). With no ``GOV_NOTIFY_API_KEY`` nothing is sent and the
log says what would have been: the email, the number of recipients, never an address. The template ids
are settings (``GOV_NOTIFY_TEMPLATE_ACCESS_REQUEST``, ``GOV_NOTIFY_TEMPLATE_ACCESS_DECIDED``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlmodel import Session, select

from transcribe_api.domain.auth.permissions import ROLE_JURISDICTION_ADMIN
from transcribe_api.domain.models_dictation import AccessRequest, AccessRequestRole, Jurisdiction, RoleAssignment, User
from transcribe_api.domain.notify import gov_notify
from transcribe_api.runtime import db_connection
from transcribe_api.runtime.settings_dictation import get_settings

logger = logging.getLogger(__name__)

EMAIL_NEW_REQUEST = "access_request"
EMAIL_DECIDED = "access_request_decided"


@dataclass(frozen=True)
class Recipient:
    email: str
    personalisation: dict[str, str]


def _app_url() -> str:
    url = get_settings().APP_URL.strip().rstrip("/")
    return url if "://" in url else f"https://{url}"


def approvals_link(request_id: UUID | str) -> str:
    """Opens the request in the approvals queue; the ID is all it carries."""
    return f"{_app_url()}/admin?tab=approvals&request={request_id}"


def _template_id(kind: str) -> str | None:
    settings = get_settings()
    return settings.GOV_NOTIFY_TEMPLATE_ACCESS_REQUEST if kind == EMAIL_NEW_REQUEST else settings.GOV_NOTIFY_TEMPLATE_ACCESS_DECIDED


def _roles_text(roles: list[str]) -> str:
    return ", ".join(sorted(roles))


def dispatch(kind: str, recipients: list[Recipient]) -> int:
    """Send one email to each recipient; returns how many were sent. Never raises."""
    if not recipients:
        logger.info("Notify email %s: nobody to send it to", kind)
        return 0
    try:
        client = gov_notify.notifications_client()
        if client is None:
            logger.info(
                "GOV.UK Notify is not configured: would send email=%s template=%s to %d recipient(s)",
                kind,
                _template_id(kind) or "(no template id set)",
                len(recipients),
            )
            return 0
        template_id = _template_id(kind)
        if not template_id:
            logger.warning("Notify email %s not sent: its template id is not set", kind)
            return 0
    except Exception as exc:
        logger.warning("Notify email %s not sent (%s)", kind, type(exc).__name__)
        return 0
    sent = 0
    for recipient in recipients:
        try:
            client.send_email_notification(
                email_address=recipient.email, template_id=template_id, personalisation=recipient.personalisation
            )
            sent += 1
        except Exception as exc:
            logger.warning("Notify email %s to one recipient failed (%s)", kind, type(exc).__name__)
    logger.info("Notify email %s sent to %d of %d recipient(s)", kind, sent, len(recipients))
    return sent


def _load(session: Session, request_id: UUID) -> tuple[AccessRequest, Jurisdiction, User, list[str]] | None:
    request = session.get(AccessRequest, request_id)
    if request is None:
        return None
    jurisdiction = session.get(Jurisdiction, request.jurisdiction_id)
    requester = session.get(User, request.user_id)
    if jurisdiction is None or requester is None:
        return None
    roles = list(session.exec(select(AccessRequestRole.role).where(AccessRequestRole.access_request_id == request.id)).all())
    return request, jurisdiction, requester, roles


def approver_recipients(session: Session, request: AccessRequest, jurisdiction: Jurisdiction, requester: User, roles: list[str]) -> list[Recipient]:
    """Active JurisdictionAdministrators of the jurisdiction: distinct users, one address each, never
    the requester."""
    rows = session.exec(
        select(User)
        .join(RoleAssignment, RoleAssignment.user_id == User.id)
        .where(
            User.is_active,
            RoleAssignment.role == ROLE_JURISDICTION_ADMIN,
            RoleAssignment.jurisdiction_id == jurisdiction.id,
        )
        .distinct()
    ).all()
    personalisation = {
        "requester_name": requester.name or requester.email,
        "jurisdiction_name": jurisdiction.name,
        "roles": _roles_text(roles),
        "link": approvals_link(request.id),
    }
    seen: set[str] = set()
    recipients = []
    for user in sorted(rows, key=lambda u: str(u.id)):
        address = (user.email or "").strip()
        if user.id == requester.id or not address or address.lower() in seen:
            continue
        seen.add(address.lower())
        recipients.append(Recipient(address, dict(personalisation)))
    return recipients


def notify_approvers(request_id: UUID) -> None:
    """Tell the approvers of a new request. Run after the response; never raises."""
    try:
        with Session(db_connection.get_engine()) as session:
            loaded = _load(session, request_id)
            if loaded is None:
                return
            request, jurisdiction, requester, roles = loaded
            recipients = approver_recipients(session, request, jurisdiction, requester, roles)
        dispatch(EMAIL_NEW_REQUEST, recipients)
    except Exception as exc:
        logger.warning("Notify email %s not sent (%s)", EMAIL_NEW_REQUEST, type(exc).__name__)


def notify_requester(request_id: UUID) -> None:
    """Tell the requester their request was approved or rejected. Run after the response; never
    raises. Nothing for a request that is pending or was withdrawn."""
    try:
        with Session(db_connection.get_engine()) as session:
            loaded = _load(session, request_id)
            if loaded is None:
                return
            request, jurisdiction, requester, roles = loaded
            if request.status not in ("approved", "rejected") or not requester.is_active:
                return
            address = (requester.email or "").strip()
            recipients = (
                [
                    Recipient(
                        address,
                        {
                            "jurisdiction_name": jurisdiction.name,
                            "roles": _roles_text(roles),
                            "outcome": request.status,
                            "link": f"{_app_url()}/",
                        },
                    )
                ]
                if address
                else []
            )
        dispatch(EMAIL_DECIDED, recipients)
    except Exception as exc:
        logger.warning("Notify email %s not sent (%s)", EMAIL_DECIDED, type(exc).__name__)
