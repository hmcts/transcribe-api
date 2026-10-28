"""Security events (DIAAT-285): a user reached, or tried to reach, a record they do not own.

One event, ``security.ownership_violation``, routed to Security by an alert
(transcribe-shared-infrastructure, the observability module's ALERTS.md), for both
areas. Attributes, all controlled values (registered in runtime/telemetry_schema.py):

* ``security.resource_type``: dictation ``transcription``, ``minute_version``,
  ``transcription_job``, ``document``, ``audio``, ``draft``; recording ``job``,
  ``transcript``, ``audio``, ``upload``;
* ``security.action``: ``read``, ``write``, ``delete``, ``reassign`` (a save that would
  move a dictation record to another owner, SEC-1), ``submit`` (a recording job naming
  another user's upload);
* ``security.outcome``: ``blocked`` (an ownership check refused the request),
  ``detected`` (a post-authorisation assertion found a record of another owner about to
  be returned: with correct code this never happens; it was blocked too), ``test`` (the
  controlled alert test, ``POST /api/security/alert-test``).

The event carries the journey (the current one, or the target job's or upload's) and
``hmcts.request.id`` (added by hmcts-otel), and nothing else: no user ID, owner ID,
email, record ID, blob or file name, title or content. Investigators correlate through
``hmcts.request.id`` with the application's logs (``UNAUTHORISED_ACCESS_ATTEMPT``) and,
once DIAAT-286 adds them, the audit records, which say who and what.
"""

from __future__ import annotations

import logging

from hmcts_otel import Journey, record_event

logger = logging.getLogger(__name__)

EVENT_OWNERSHIP_VIOLATION = "security.ownership_violation"
ATTR_RESOURCE_TYPE = "security.resource_type"
ATTR_ACTION = "security.action"
ATTR_OUTCOME = "security.outcome"

# Dictation
TRANSCRIPTION = "transcription"
MINUTE_VERSION = "minute_version"
TRANSCRIPTION_JOB = "transcription_job"
DOCUMENT = "document"
AUDIO = "audio"
DRAFT = "draft"
# Recording
JOB = "job"
TRANSCRIPT = "transcript"
UPLOAD = "upload"

READ = "read"
WRITE = "write"
DELETE = "delete"
REASSIGN = "reassign"
SUBMIT = "submit"

BLOCKED = "blocked"
DETECTED = "detected"
TEST = "test"

# OwnershipConflictError carries the dictation model name (SEC-1).
_MODEL_RESOURCE_TYPES = {
    "Transcription": TRANSCRIPTION,
    "MinuteVersion": MINUTE_VERSION,
    "TranscriptionJob": TRANSCRIPTION_JOB,
    "LiveTranscriptDraft": DRAFT,
}


def record_ownership_violation(
    resource_type: str,
    action: str,
    outcome: str = BLOCKED,
    *,
    journey: Journey | None = None,
) -> None:
    """Record ``security.ownership_violation``: the single place that does.

    Only the three enums, plus the request ID and the journey that record_event adds
    (``journey`` if given, e.g. the target job, else the current one). Nothing about
    who the caller or the owner is. ``detected`` is also logged at ERROR (types only):
    it means an authorisation check let a non-owner through.
    """
    record_event(
        EVENT_OWNERSHIP_VIOLATION,
        {ATTR_RESOURCE_TYPE: resource_type, ATTR_ACTION: action, ATTR_OUTCOME: outcome},
        journey=journey,
    )
    if outcome == DETECTED:
        logger.error("OWNERSHIP_ASSERTION_FAILED resource_type=%s action=%s", resource_type, action)


def resource_type_of(model_name: str) -> str:
    """The resource type for a dictation model name (``OwnershipConflictError.record_type``).

    An unmapped model is reported as a transcription, and logged, so a new SEC-1 guard
    without a resource type shows up rather than being silently misreported.
    """
    resource_type = _MODEL_RESOURCE_TYPES.get(model_name)
    if resource_type is None:
        logger.warning("No security resource type for model %s; reported as transcription", model_name)
        return TRANSCRIPTION
    return resource_type


def owner_matches(owner_id: object, user_id: object, *, resource_type: str, action: str = READ) -> bool:
    """Post-authorisation assertion: is the record about to be returned the caller's?

    Called after the authorised lookup, just before a record leaves the service. The
    lookup already filters by owner, so this is always True with correct code. When it
    is not, records ``detected`` and returns False: the caller refuses the request
    exactly as it refuses an unknown ID.
    """
    if owner_id is not None and str(owner_id) == str(user_id):
        return True
    record_ownership_violation(resource_type, action, DETECTED)
    return False
