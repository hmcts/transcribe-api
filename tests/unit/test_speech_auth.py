"""Speech REST calls authenticate with the pod's workload identity.

AAT's Speech account has key auth disabled and no key is mounted, so the
batch and fast transcription clients send an Entra bearer token. The key
header is used only when AZURE_SPEECH_KEY is set (local development).
"""

from __future__ import annotations

import pytest

from transcribe_api.stt import speech_auth
from transcribe_api.stt.speech_auth import SPEECH_SCOPE, SpeechAuthError, speech_auth_headers


@pytest.mark.asyncio
async def test_bearer_token_from_workload_identity_when_no_key(speech_workload_identity):
    headers = await speech_auth_headers("")

    assert headers == {"Authorization": "Bearer aad-token"}
    assert speech_workload_identity.scopes == [(SPEECH_SCOPE,)]
    assert SPEECH_SCOPE == "https://cognitiveservices.azure.com/.default"


@pytest.mark.asyncio
async def test_one_credential_is_shared_across_calls(speech_workload_identity):
    await speech_auth_headers("")
    await speech_auth_headers("")

    assert type(speech_workload_identity).instances == 1
    assert len(speech_workload_identity.scopes) == 2


@pytest.mark.asyncio
async def test_key_header_when_a_key_is_configured(speech_workload_identity):
    headers = await speech_auth_headers("local-dev-key")

    assert headers == {"Ocp-Apim-Subscription-Key": "local-dev-key"}
    assert speech_workload_identity.scopes == []


@pytest.mark.asyncio
async def test_credential_failure_raises_a_clear_error(speech_workload_identity):
    speech_workload_identity.error = RuntimeError("no federated token file")

    with pytest.raises(SpeechAuthError, match="workload identity") as exc:
        await speech_auth_headers("")

    assert "no federated token file" in str(exc.value)
    assert speech_auth.SpeechAuthError is SpeechAuthError
