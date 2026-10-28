"""DIAAT-205: a recording upload exports no case reference or email (OBS-2).

The redaction rules themselves are tested in tests/unit/test_telemetry_setup.py.
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient
from hmcts_otel.testing import capture_telemetry

APPEAL_FILE = "PA_50123_2024"


def test_upload_exports_no_case_reference_or_email(mocker, monkeypatch):
    """The original file name is a case reference; no exported telemetry may carry it."""
    from transcribe_api.api.app import create_app
    from transcribe_api.domain.auth.dependencies_recording import get_current_user
    from transcribe_api.domain.models_recording import User
    from transcribe_api.runtime import settings_dictation

    manager = mocker.AsyncMock()
    manager.create_blob_from_bytes = mocker.AsyncMock(return_value=True)
    manager.build_blob_url = mocker.Mock(return_value="https://x/y.wav")
    manager.__aenter__ = mocker.AsyncMock(return_value=manager)
    manager.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("transcribe_api.api.routes_recording.AsyncAzureBlobManager", return_value=manager)
    mocker.patch(
        "transcribe_api.api.routes_recording.normalize_to_wav",
        new=mocker.AsyncMock(return_value=b"WAVDATA"),
    )
    # The suite turns FastAPI instrumentation off; this test needs the request span.
    monkeypatch.setenv("DISABLE_FASTAPI_INSTRUMENTATION", "false")
    settings_dictation.get_settings.cache_clear()

    with capture_telemetry() as telemetry:
        app = create_app()
        user = mocker.Mock(spec=User)
        user.id = "00000000-0000-0000-0000-000000000001"
        user.email = "judge.smith@justice.gov.uk"
        app.dependency_overrides[get_current_user] = lambda: user
        response = TestClient(app).post(
            "/api/v1/uploads",
            files={"file": (f"{APPEAL_FILE}.mp3", b"fake-audio-bytes", "audio/mpeg")},
        )
        logging.getLogger("transcribe_api").info("processed upload for %s", user.email)

        assert response.status_code == 201, response.text
        assert response.headers.get("X-Request-Id")
        telemetry.assert_no_prohibited_content(APPEAL_FILE, "judge.smith@justice.gov.uk")
        (server,) = [s for s in telemetry.finished_spans() if s.kind.name == "SERVER"]
        # Route template, not the concrete path: names and IDs in paths are not exported.
        assert server.attributes.get("http.route") == "/api/v1/uploads"
    settings_dictation.get_settings.cache_clear()
