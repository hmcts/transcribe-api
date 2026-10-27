"""Regression sentinel: save_live_transcription_submission must not log PII.

DIAAT-298: the endpoint previously dumped the full submission payload verbatim
to Application Insights via json.dumps(payload, indent=2), exposing transcripts,
judicial notes, party names, judge name, and hearing date to the shared
hmcts-prod Log Analytics workspace. 123 submissions from 56 judges were affected.
"""

import logging
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from transcribe_api.domain.models_dictation import User

# Sentinel strings that must NEVER appear in any log record.
PII_APPELLANT = "Okonkwo-DIAAT298"
PII_RESPONDENT = "HomeOfficeRespondent-DIAAT298"
PII_JUDGE = "HHJ-Blackwood-DIAAT298"
PII_NOTE = "PrivateJudicialNote-DIAAT298"
PII_TRANSCRIPT = "AppellantStatesDIAAT298sentinel"

_MINIMAL_BODY = {
    "form_data": {
        "location": "Birmingham",
        "hearingDate": "2026-01-15",
        "judgeName": PII_JUDGE,
        "appellantName": PII_APPELLANT,
        "respondent": PII_RESPONDENT,
        "hearingType": "substantive",
        "appealableDecisionDate": "2025-06-01",
        "documentType": "determination",
    },
    "messages": {
        "background": [
            {
                "speaker": "Judge",
                "text": PII_TRANSCRIPT,
                "timestamp": "00:00:00",
                "timestampMs": 0,
            }
        ],
        "evidence": [],
        "facts": [],
    },
    "notes": PII_NOTE,
}

PII_EMAIL = "judge@justice.gov.uk"

_PII_SENTINELS = [PII_APPELLANT, PII_RESPONDENT, PII_JUDGE, PII_NOTE, PII_TRANSCRIPT, PII_EMAIL]


def _make_user(roles=None):
    user = User(id=uuid.uuid4(), email="judge@justice.gov.uk", azure_user_id=str(uuid.uuid4()))
    user.__dict__["app_roles"] = roles or []
    user.has_completed_onboarding = True
    return user


def _make_blob_manager_mock():
    blob_manager = AsyncMock()
    blob_manager.create_blob_from_file = AsyncMock(return_value=True)
    blob_manager.set_blob_metadata = AsyncMock(return_value=True)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=blob_manager)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.fixture
def app():
    import transcribe_api.api.routes_dictation
    test_app = FastAPI()
    test_app.include_router(transcribe_api.api.routes_dictation.router, prefix="/api")
    return test_app


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def as_user(app):
    from transcribe_api.domain.auth.dependencies_dictation import get_current_user
    yield lambda user: app.dependency_overrides.__setitem__(get_current_user, lambda: user)
    app.dependency_overrides.pop(get_current_user, None)


def _patch_submission_deps(mocker, *, direct_sdk_access: bool = False):
    """Patch the heavy dependencies of save_live_transcription_submission."""
    mocker.patch("transcribe_api.api.routes_dictation.AsyncAzureBlobManager", return_value=_make_blob_manager_mock())

    mock_doc_path = MagicMock(spec=Path)
    mock_doc_path.__str__ = lambda _: "/tmp/test-doc.docx"
    mock_doc_path.exists.return_value = False
    mocker.patch("transcribe_api.api.routes_dictation.render_hearing_document", return_value=mock_doc_path)

    saved_transcription = MagicMock()
    saved_transcription.id = uuid.uuid4()
    mocker.patch("transcribe_api.api.routes_dictation.save_transcription", return_value=saved_transcription)
    mocker.patch("transcribe_api.api.routes_dictation.save_transcription_job")

    mock_settings = MagicMock()
    mock_settings.DIRECT_SDK_ACCESS = direct_sdk_access
    mock_settings.APP_URL = "http://localhost:8000"
    mock_settings.DOWNLOAD_URL_TTL_MINUTES = 60
    mock_settings.AZURE_STORAGE_CONTAINER_NAME = "test-container"
    mocker.patch("transcribe_api.api.routes_dictation.get_settings", return_value=mock_settings)

    if direct_sdk_access:
        mocker.patch(
            "transcribe_api.api.routes_dictation.generate_blob_download_url",
            return_value="https://storage.example.com/doc?sas=token",
        )

    return saved_transcription


def _assert_no_pii_in_logs(caplog):
    # Include hmcts_azure_auth.dependencies — that logger now has an email-redaction
    # filter (applied by utils/dependencies.py at import time) so it no longer emits
    # raw email addresses to any handler.  Including it here verifies the filter is
    # effective end-to-end; omitting it would leave role-check denials untested.
    our_records = [
        r for r in caplog.records
        if r.name.startswith(("transcribe_api.", "hmcts_azure_auth."))
    ]
    all_log_text = " ".join(r.getMessage() for r in our_records)
    for sentinel in _PII_SENTINELS:
        assert sentinel not in all_log_text, (
            f"PII sentinel '{sentinel}' must not appear in any log record from app/api loggers"
        )


def test_submission_does_not_log_pii(client, as_user, mocker, caplog):
    """PII strings from the request body must not appear in any log record."""
    user = _make_user(roles=["Judge"])
    as_user(user)
    _patch_submission_deps(mocker)

    with caplog.at_level(logging.DEBUG):
        response = client.post("/api/live-transcription/submission", json=_MINIMAL_BODY)

    assert response.status_code == 200, (
        f"Expected 200 so log assertions are non-vacuous, got {response.status_code}: {response.text}"
    )
    _assert_no_pii_in_logs(caplog)


def test_submission_does_not_log_pii_sdk_branch(client, as_user, mocker, caplog):
    """PII must not appear in logs when DIRECT_SDK_ACCESS=True (the SDK download-URL branch)."""
    user = _make_user(roles=["Judge"])
    as_user(user)
    _patch_submission_deps(mocker, direct_sdk_access=True)

    with caplog.at_level(logging.DEBUG):
        response = client.post("/api/live-transcription/submission", json=_MINIMAL_BODY)

    assert response.status_code == 200, (
        f"Expected 200 so log assertions are non-vacuous, got {response.status_code}: {response.text}"
    )
    _assert_no_pii_in_logs(caplog)


def test_hmcts_azure_auth_email_filter(caplog):
    """_RedactEmailFilter must be attached by the module-level loop to every hmcts_azure_auth.* logger.

    Observed in production: 'JWT verification passed for user: x@justice.gov.uk',
    'Role check passed for user x@justice.gov.uk', 'Authentication validated — email=...'
    all reached Application Insights unredacted because Python logging filters do NOT
    propagate from a parent logger to child loggers — hmcts_azure_auth.dependencies was
    the only logger covered, while hmcts_azure_auth.jwt, .roles, .auth etc. were not.

    Test strategy: create the sub-loggers first (so they exist in loggerDict), then
    reload utils.dependencies to re-run the production module-level registration loop,
    then emit and assert.  This proves the PRODUCTION code path attaches the filter —
    not a manual addFilter call in the test — so a regression in the loop would be caught.
    """
    import importlib
    import logging as _logging

    from transcribe_api.domain.auth import dependencies_dictation as _deps_module

    # Synthetic addresses — not real people's data.
    # Cover local parts beyond [a-z0-9._%+-]: RFC 5322 permits ! # $ % & ' * + / = ?
    # ^ _ ` { | } ~ and '.'. These should still be redacted.
    sentinels = [
        "sentinel@example.test",
        "o'brien@example.test",
        "foo!bar@example.test",
        "a.b#c$d%e&f@example.test",
        "user+tag@example.test",
    ]
    # The three sub-loggers that emitted PII in the live incident.
    # Create them BEFORE reloading so the registration loop finds them in loggerDict.
    logger_names = [
        "hmcts_azure_auth.dependencies",
        "hmcts_azure_auth.jwt",
        "hmcts_azure_auth.roles",
    ]
    for name in logger_names:
        _logging.getLogger(name)

    # Reload the module to re-run the module-level registration loop — this is the
    # production code under test.  If the loop is broken the filters won't be attached
    # and the assertions below will fail.
    importlib.reload(_deps_module)

    import transcribe_api.api.routes_dictation as _routes_module

    # Emit one message per logger, each containing all sentinel addresses so the
    # filter is exercised against every RFC 5322 local-part variant in a single pass.
    sentinel_msg = " and ".join(sentinels)
    try:
        with caplog.at_level(logging.INFO):
            _logging.getLogger("hmcts_azure_auth.dependencies").info(
                "Found existing user: %s", sentinel_msg
            )
            _logging.getLogger("hmcts_azure_auth.jwt").info(
                "JWT verification passed for user: %s", sentinel_msg
            )
            _logging.getLogger("hmcts_azure_auth.roles").info(
                "Role check passed for user %s (roles=['Judge'])", sentinel_msg
            )

        auth_records = [
            r for r in caplog.records if r.name.startswith("hmcts_azure_auth.")
        ]
        assert len(auth_records) == 3, f"Expected 3 auth log records, got {len(auth_records)}"
        for record in auth_records:
            msg = record.getMessage()
            for sentinel in sentinels:
                assert sentinel not in msg, (
                    f"_RedactEmailFilter must cover {record.name} — "
                    f"'{sentinel}' must not appear. Got: {msg!r}"
                )
            assert "[redacted]" in msg, (
                f"Expected '[redacted]' in {record.name} record. Got: {msg!r}"
            )
    finally:
        # Reloading utils.dependencies created a new get_current_user object, which
        # breaks subsequent tests whose app fixture uses api.routes' stale reference.
        # Run unconditionally so a failing assertion above does not leave later tests
        # with a stale dependency reference.
        importlib.reload(_routes_module)


def test_submission_logs_submission_id_and_user_id(client, as_user, mocker, caplog):
    """The endpoint must emit audit-safe identifiers so submissions are traceable."""
    user = _make_user(roles=["Judge"])
    as_user(user)
    _patch_submission_deps(mocker)

    with caplog.at_level(logging.INFO):
        response = client.post("/api/live-transcription/submission", json=_MINIMAL_BODY)

    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"

    our_records = [r for r in caplog.records if r.name.startswith("transcribe_api.")]
    all_log_text = " ".join(r.getMessage() for r in our_records)
    assert "submission_id=" in all_log_text, "Expected submission_id= in application logs"
    assert "user_id=" in all_log_text, "Expected user_id= in application logs"
