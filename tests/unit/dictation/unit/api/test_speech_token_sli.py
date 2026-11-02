"""OBS-4: token-mint latency and failures are recorded as a stage (an SLI), and the
error text never reaches the client or the logs."""

from unittest.mock import MagicMock

from hmcts_otel import conventions as c
from hmcts_otel.testing import capture_telemetry

from tests.unit.dictation.unit.api.test_routes import _make_user_with_oid

SECRET = "key=" + "abc123" + "SECRET"


def _settings(mocker, **values):
    settings = MagicMock(
        AZURE_SPEECH_ENDPOINT="https://speech.example", AZURE_SPEECH_RESOURCE_ID=None, AZURE_SPEECH_KEY="k"
    )
    for key, value in values.items():
        setattr(settings, key, value)
    mocker.patch("transcribe_api.api.routes_dictation.get_settings", return_value=settings)


def test_token_mint_success_is_timed(client, as_user, mocker):
    as_user(_make_user_with_oid())
    _settings(mocker)
    response = MagicMock(text="token")
    http = mocker.AsyncMock()
    http.post = mocker.AsyncMock(return_value=response)
    http.__aenter__ = mocker.AsyncMock(return_value=http)
    http.__aexit__ = mocker.AsyncMock(return_value=False)
    mocker.patch("transcribe_api.api.routes_dictation.httpx.AsyncClient", return_value=http)
    with capture_telemetry() as telemetry:
        assert client.get("/api/get-speech-token").status_code == 200
        (done,) = telemetry.events("speech_token.completed")
        assert done[c.OUTCOME] == "success"


def test_token_mint_failure_is_an_error_without_its_message(client, as_user, mocker):
    as_user(_make_user_with_oid())
    _settings(mocker)
    mocker.patch("transcribe_api.api.routes_dictation.httpx.AsyncClient", side_effect=RuntimeError(SECRET))
    with capture_telemetry() as telemetry:
        response = client.get("/api/get-speech-token")
        assert response.status_code == 500
        assert SECRET not in response.text
        (failed,) = telemetry.events("speech_token.failed")
        assert failed[c.OUTCOME] == "error"
        assert failed[c.ERROR_CODE] == "speech_token.request_failed"
        telemetry.assert_no_prohibited_content(SECRET)


def test_missing_configuration_is_reported_once_with_the_same_responses(client, as_user, mocker):
    as_user(_make_user_with_oid())
    for values, detail in (
        ({"AZURE_SPEECH_ENDPOINT": None}, "Azure Speech Services not configured"),
        (
            {"AZURE_SPEECH_KEY": None},
            "Azure Speech Services not configured: set AZURE_SPEECH_KEY or AZURE_SPEECH_RESOURCE_ID",
        ),
    ):
        _settings(mocker, **values)
        with capture_telemetry() as telemetry:
            response = client.get("/api/get-speech-token")
            assert response.status_code == 500
            assert response.json()["detail"] == detail
            (failed,) = telemetry.events("speech_token.failed")
            assert failed[c.ERROR_CODE] == "speech_token.not_configured"
