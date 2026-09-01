"""Persist audit events emitted by the hmcts-fastapi-azure-auth library.

Pass write_audit_event as the audit_writer kwarg to get_allowlisted_user()
on any route where denied-access attempts should be recorded in the DB.
"""

from __future__ import annotations

import logging

from sqlmodel import Session

from transcribe_api.runtime.db_connection import get_engine
from transcribe_api.domain.models_dictation import AuditLog
from hmcts_azure_auth.audit import AuditEvent

logger = logging.getLogger(__name__)


def write_audit_event(event: AuditEvent) -> None:
    """Write a single AuditEvent to the audit_log table.

    Called synchronously by get_allowlisted_user on every 403. Failures are
    logged but do not propagate — a logging failure must never block a response.
    """
    try:
        with Session(get_engine()) as session:
            log = AuditLog(
                timestamp=event.timestamp,
                event_type=event.event_type,
                user_id=event.user_id,
                email=event.email,
                held_roles=event.held_roles,
                required_roles=event.required_roles,
                resource=event.resource,
                detail=event.detail,
                client_ip=event.client_ip,
            )
            session.add(log)
            session.commit()
    except Exception:
        logger.exception("Failed to write audit event type=%s user_id=%s", event.event_type, event.user_id)
