"""DIAAT-285: security.ownership_violation, the event behind the Security alert.

* blocked: an authorisation check refused a resource the caller does not own;
* detected: the post-authorisation ownership assertion caught one that authorisation
  let through (a defect; the caller still gets the usual 404);
* test: the dev-only controlled trigger for the alert route.

Responses are unchanged, and the event never carries user IDs, emails, blob names,
file names or content: only the three enums, the request ID and the target's journey.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from hmcts_otel import conventions as c
from hmcts_otel import default_schema
from hmcts_otel.testing import capture_telemetry

from transcribe_api.domain.models_recording import JobStatus, SpeechBatchJob, User
from transcribe_api.runtime.security_events import (
    ATTR_ACTION as ATTR_SECURITY_ACTION,
)
from transcribe_api.runtime.security_events import (
    ATTR_OUTCOME as ATTR_SECURITY_OUTCOME,
)
from transcribe_api.runtime.security_events import (
    ATTR_RESOURCE_TYPE as ATTR_SECURITY_RESOURCE_TYPE,
)
from transcribe_api.runtime.security_events import (
    EVENT_OWNERSHIP_VIOLATION,
    record_ownership_violation,
)
from transcribe_api.runtime.telemetry import JOURNEY_JOB, JOURNEY_UPLOAD, configure_telemetry

ROUTES = "transcribe_api.api.routes_recording"
CALLER_ID = uuid.UUID("00000000-0000-4000-8000-0000000000c1")
CALLER_EMAIL = "caller@example.com"
OWNER_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a2")
UPLOAD_ID = "6f1c2d3e-4b5a-4c6d-8e7f-001122334455"
OTHER_BLOB = f"uploads/{OWNER_ID}/{UPLOAD_ID}-PA_50123_2024 Smith v Home Office.wav"
TRANSCRIPT_TEXT = "the appellant said something confidential"
# Every value that would identify a person or carry content; none may be exported.
PROHIBITED = (
    str(CALLER_ID),
    CALLER_EMAIL,
    "caller-azure-oid",
    str(OWNER_ID),
    OTHER_BLOB,
    "Smith v Home Office",
    "PA_50123_2024",
    TRANSCRIPT_TEXT,
    "hearing.wav",
)


@pytest.fixture
def tel():
    with capture_telemetry() as capture:
        configure_telemetry()
        yield capture


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    from transcribe_api.api.routes_recording import limiter

    limiter.reset()
    yield
    limiter.reset()


def _job(user_id: uuid.UUID | None = OWNER_ID, text: str = TRANSCRIPT_TEXT) -> SpeechBatchJob:
    job = SpeechBatchJob(
        id=uuid.uuid4(),
        caller_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        user_id=user_id,
        audio_url="https://storage.example.com/x/hearing.wav",
        audio_blob_path=OTHER_BLOB,
        locale="en-GB",
        status=JobStatus.SUCCEEDED,
        metadata_={},
        dialogue_entries=[{"speaker": "Speaker 1", "text": text, "start_time": 0.0, "end_time": 1.0}],
    )
    job.created_datetime = datetime(2026, 1, 1, tzinfo=UTC)
    return job


def _client(roles: list[str] | None = None) -> TestClient:
    from transcribe_api.api.app import create_app
    from transcribe_api.domain.auth.auth_models_recording import AuthenticatedUser
    from transcribe_api.domain.auth.dependencies_recording import get_current_user

    user = User(
        id=CALLER_ID,
        azure_user_id="caller-azure-oid",
        email=CALLER_EMAIL,
        role="Normal",
        created_datetime=datetime(2026, 1, 1, tzinfo=UTC),
    )
    app = create_app()
    current = AuthenticatedUser(db_user=user, app_roles=roles or ["Normal"])
    app.dependency_overrides[get_current_user] = lambda: current
    return TestClient(app, raise_server_exceptions=False)


def _violations(tel) -> list[dict]:
    return tel.events(EVENT_OWNERSHIP_VIOLATION)


def _assert_types(event: dict, resource_type: str, action: str, outcome: str) -> None:
    assert event[ATTR_SECURITY_RESOURCE_TYPE] == resource_type
    assert event[ATTR_SECURITY_ACTION] == action
    assert event[ATTR_SECURITY_OUTCOME] == outcome
    assert event.get(c.REQUEST_ID), "investigators correlate on the request ID"


def _assert_no_pii(tel) -> None:
    tel.assert_no_prohibited_content(*PROHIBITED)
    for event in _violations(tel):
        allowed = {
            ATTR_SECURITY_RESOURCE_TYPE,
            ATTR_SECURITY_ACTION,
            ATTR_SECURITY_OUTCOME,
            c.REQUEST_ID,
            c.JOURNEY_TYPE,
            c.JOURNEY_ID,
            c.CUSTOM_EVENT_NAME,
            c.EVENT_CLASS,
            c.EVENT_SOURCE,
        }
        extra = {k for k in event if k not in allowed and not k.startswith(("code.", "otel"))}
        assert not extra, f"unexpected attributes on the security event: {sorted(extra)}"


# blocked: POST /jobs with another user's blob ------------------------------------------


def test_foreign_blob_name_is_blocked_with_the_same_422_and_a_security_event(tel, mocker):
    mocker.patch(f"{ROUTES}.get_job_by_idempotency_key_for_user", return_value=None)
    submit = mocker.patch(f"{ROUTES}.submit_and_queue_batch_job")

    response = _client().post(
        "/api/v1/jobs", json={"audio_url": "https://x.example.com/a.wav", "blob_name": OTHER_BLOB}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "blob_name does not belong to this user"
    submit.assert_not_called()
    (event,) = _violations(tel)
    _assert_types(event, "upload", "submit", "blocked")
    assert event[c.JOURNEY_TYPE] == JOURNEY_UPLOAD
    assert event[c.JOURNEY_ID] == UPLOAD_ID  # the targeted upload, never its name
    _assert_no_pii(tel)


def test_own_blob_name_is_not_a_violation(tel, mocker):
    mocker.patch(f"{ROUTES}.get_job_by_idempotency_key_for_user", return_value=None)
    mocker.patch(f"{ROUTES}.submit_and_queue_batch_job", return_value=_job(CALLER_ID))

    response = _client().post(
        "/api/v1/jobs",
        json={
            "audio_url": "https://x.example.com/a.wav",
            "blob_name": f"uploads/{CALLER_ID}/a.wav",
        },
    )

    assert response.status_code == 201
    assert _violations(tel) == []


# blocked: job lookups by ID ----------------------------------------------------------------


def _call(client: TestClient, route: str, job_id: uuid.UUID):
    if route == "get":
        return client.get(f"/api/v1/jobs/{job_id}")
    if route == "audio":
        return client.get(f"/api/v1/jobs/{job_id}/audio")
    if route == "delete":
        return client.delete(f"/api/v1/jobs/{job_id}")
    if route == "correct":
        return client.patch(f"/api/v1/jobs/{job_id}/segments/0", json={"corrected_text": "x"})
    if route == "baseline":
        return client.post(f"/api/v1/jobs/{job_id}/baseline", files={"file": ("b.txt", b"hello", "text/plain")})
    raise AssertionError(route)


@pytest.mark.parametrize(
    ("route", "resource_type", "action"),
    [
        ("get", "transcript", "read"),
        ("audio", "audio", "read"),
        ("delete", "job", "delete"),
        ("correct", "transcript", "write"),
        ("baseline", "transcript", "write"),
    ],
)
@pytest.mark.parametrize("owner", [OWNER_ID, None], ids=["other_user", "legacy"])
def test_another_users_job_is_blocked_with_the_not_found_response(tel, mocker, route, resource_type, action, owner):
    job = _job(owner)
    client = _client()
    mocker.patch(f"{ROUTES}.get_job_by_id", return_value=None)
    not_found = _call(client, route, job.id)
    mocker.patch(f"{ROUTES}.get_job_by_id", return_value=job)

    response = _call(client, route, job.id)

    # Same status and body as a job that does not exist: no enumeration (CNP adds
    # each response's own request_id to error bodies).
    assert (response.status_code, response.json()["detail"]) == (404, "Job not found")
    assert (not_found.status_code, not_found.json()["detail"]) == (404, "Job not found")
    assert response.json().keys() == not_found.json().keys()
    (event,) = _violations(tel)  # the unknown job emitted nothing
    _assert_types(event, resource_type, action, "blocked")
    assert event[c.JOURNEY_TYPE] == JOURNEY_JOB
    assert event[c.JOURNEY_ID] == str(job.id)
    _assert_no_pii(tel)


@pytest.mark.parametrize(("owner", "roles"), [(CALLER_ID, None), (OWNER_ID, ["SystemAdministrator"])])
def test_owner_and_administrator_reads_are_not_violations(tel, mocker, owner, roles):
    mocker.patch(f"{ROUTES}.get_job_by_id", return_value=_job(owner))

    response = _client(roles).get(f"/api/v1/jobs/{uuid.uuid4()}")

    assert response.status_code == 200
    assert _violations(tel) == []


# detected: the post-authorisation assertion ------------------------------------------------


@pytest.mark.parametrize(("route", "resource_type"), [("get", "transcript"), ("audio", "audio")])
def test_assertion_blocks_what_a_broken_authorisation_check_lets_through(tel, mocker, caplog, route, resource_type):
    # Simulate a regression in the authorisation check: it lets everyone through.
    mocker.patch(f"{ROUTES}._check_job_access")
    job = _job(OWNER_ID)
    mocker.patch(f"{ROUTES}.get_job_by_id", return_value=job)

    with caplog.at_level(logging.ERROR, logger="transcribe_api.runtime.security_events"):
        response = _call(_client(), route, job.id)

    assert (response.status_code, response.json()["detail"]) == (404, "Job not found")
    assert TRANSCRIPT_TEXT not in response.text
    (event,) = _violations(tel)
    _assert_types(event, resource_type, "read", "detected")
    assert event[c.JOURNEY_ID] == str(job.id)
    (record,) = [r for r in caplog.records if r.name == "transcribe_api.runtime.security_events"]
    assert record.levelno == logging.ERROR
    assert record.getMessage() == f"OWNERSHIP_ASSERTION_FAILED resource_type={resource_type} action=read"
    _assert_no_pii(tel)


def test_assertion_lets_an_administrator_through(tel, mocker):
    mocker.patch(f"{ROUTES}._check_job_access")
    mocker.patch(f"{ROUTES}.get_job_by_id", return_value=_job(None))

    response = _client(["SystemAdministrator"]).get(f"/api/v1/jobs/{uuid.uuid4()}")

    assert response.status_code == 200
    assert _violations(tel) == []


def test_list_drops_a_foreign_row_the_user_filter_let_through(tel, mocker):
    own, foreign = _job(CALLER_ID, text="the caller's own hearing"), _job(OWNER_ID)
    mocker.patch(f"{ROUTES}.list_jobs_paginated", return_value=([own, foreign], 2))

    response = _client().get("/api/v1/jobs")

    assert response.status_code == 200
    assert [job["job_id"] for job in response.json()["jobs"]] == [str(own.id)]
    assert TRANSCRIPT_TEXT not in response.text
    (event,) = _violations(tel)
    _assert_types(event, "transcript", "read", "detected")
    assert event[c.JOURNEY_ID] == str(foreign.id)
    _assert_no_pii(tel)


def test_list_with_only_own_rows_is_not_a_violation(tel, mocker):
    mocker.patch(f"{ROUTES}.list_jobs_paginated", return_value=([_job(CALLER_ID)], 1))

    assert _client().get("/api/v1/jobs").status_code == 200
    assert _violations(tel) == []


# The helper and the schema -----------------------------------------------------------------


def test_event_is_registered_with_only_the_three_enums(tel):
    spec = default_schema.get_event(EVENT_OWNERSHIP_VIOLATION)
    assert spec is not None
    assert set(spec.attributes) == {
        ATTR_SECURITY_RESOURCE_TYPE,
        ATTR_SECURITY_ACTION,
        ATTR_SECURITY_OUTCOME,
    }
    attributes = default_schema.attributes
    # The merged schema: recording's values, alongside dictation's.
    assert {"job", "transcript", "audio", "upload"} <= attributes[ATTR_SECURITY_RESOURCE_TYPE].allowed_values
    assert {"read", "write", "delete", "submit"} <= attributes[ATTR_SECURITY_ACTION].allowed_values
    assert attributes[ATTR_SECURITY_OUTCOME].allowed_values == {"blocked", "detected", "test"}


def test_a_value_outside_the_enums_is_dropped_not_exported(tel):
    record_ownership_violation("job", "read", "blocked")
    record_ownership_violation("PA/50123/2024", "read", "blocked")  # type: ignore[arg-type]

    first, second = _violations(tel)
    assert first[ATTR_SECURITY_RESOURCE_TYPE] == "job"
    assert ATTR_SECURITY_RESOURCE_TYPE not in second
    tel.assert_no_prohibited_content("PA/50123/2024")


# The dev-only controlled trigger ------------------------------------------------------------
