"""Unit tests for the Azure Batch Transcription REST client."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _make_response(body=None, status_code=200, headers=None):
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status_code
    mock.headers = headers or {}
    mock.json.return_value = body or {}
    mock.text = str(body)
    mock.raise_for_status = MagicMock()
    return mock


class TestSubmitBatchJob:
    @pytest.mark.asyncio
    async def test_returns_job_url_on_201(self):
        from transcribe_api.stt.batch_client import submit_batch_job

        mock_response = _make_response(
            status_code=201,
            headers={
                "Location": "https://eastus.cognitiveservices.azure.com/speechtotext/transcriptions/abc-123"
            },
        )
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)

            result = await submit_batch_job("https://sas-url", "test-job")

        assert (
            result
            == "https://eastus.cognitiveservices.azure.com/speechtotext/transcriptions/abc-123"
        )

    @pytest.mark.asyncio
    async def test_raises_on_non_201(self):
        from transcribe_api.stt.batch_client import BatchSubmissionError, submit_batch_job

        mock_response = _make_response(status_code=400)
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)

            with pytest.raises(BatchSubmissionError):
                await submit_batch_job("https://sas-url", "test-job")

    @pytest.mark.asyncio
    async def test_submits_to_path_versioned_url_without_query_api_version(self):
        # Regression test: the "?api-version=..." form 404s outright against
        # a real Speech resource (confirmed on deployed dev) — the path-
        # versioned route below is the one that actually exists.
        import os

        from transcribe_api.runtime.settings_recording import get_settings
        from transcribe_api.stt.batch_client import submit_batch_job

        os.environ["AZURE_SPEECH_ENDPOINT"] = "https://my-resource.cognitiveservices.azure.com"
        get_settings.cache_clear()

        mock_response = _make_response(
            status_code=201,
            headers={
                "Location": "https://eastus.cognitiveservices.azure.com/speechtotext/transcriptions/xyz"
            },
        )
        captured_url = {}

        async def capture_post(url, headers, json):
            captured_url["value"] = url
            return mock_response

        try:
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                mock_client.post = capture_post

                await submit_batch_job("https://sas-url", "test-job")
        finally:
            get_settings.cache_clear()

        assert captured_url["value"] == (
            "https://my-resource.cognitiveservices.azure.com/speechtotext/v3.2/transcriptions"
        )

    @pytest.mark.asyncio
    async def test_includes_diarization_when_enabled(self):
        from transcribe_api.stt.batch_client import submit_batch_job

        mock_response = _make_response(
            status_code=201,
            headers={
                "Location": "https://eastus.cognitiveservices.azure.com/speechtotext/transcriptions/xyz"
            },
        )
        captured_payload = {}

        async def capture_post(url, headers, json):
            captured_payload.update(json)
            return mock_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = capture_post

            await submit_batch_job("https://sas-url", "test-job", enable_diarization=True)

        assert captured_payload["properties"]["diarizationEnabled"] is True
        assert "diarization" in captured_payload["properties"]

    @pytest.mark.asyncio
    async def test_requests_full_azure_speaker_range_when_diarization_enabled(self):
        """The submitted speaker range must not cap below what Azure allows.

        `diarizationEnabled` alone only separates two speakers, so the
        `diarization` block has to carry the range. Azure accepts a
        `maxCount` "less than 36"; asking for less silently merges the
        surplus voices into one label instead of failing the job.
        """
        from transcribe_api.stt.batch_client import submit_batch_job

        mock_response = _make_response(
            status_code=201,
            headers={
                "Location": "https://eastus.cognitiveservices.azure.com/speechtotext/transcriptions/xyz"
            },
        )
        captured_payload = {}

        async def capture_post(url, headers, json):
            captured_payload.update(json)
            return mock_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = capture_post

            await submit_batch_job("https://sas-url", "test-job", enable_diarization=True)

        diarization = captured_payload["properties"]["diarization"]
        assert diarization["enabled"] is True
        assert diarization["speakers"] == {"minCount": 1, "maxCount": 35}

    @pytest.mark.asyncio
    async def test_omits_diarization_when_disabled(self):
        from transcribe_api.stt.batch_client import submit_batch_job

        mock_response = _make_response(
            status_code=201,
            headers={
                "Location": "https://eastus.cognitiveservices.azure.com/speechtotext/transcriptions/xyz"
            },
        )
        captured_payload = {}

        async def capture_post(url, headers, json):
            captured_payload.update(json)
            return mock_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = capture_post

            await submit_batch_job("https://sas-url", "test-job", enable_diarization=False)

        assert "diarizationEnabled" not in captured_payload.get("properties", {})

    @pytest.mark.asyncio
    async def test_profanity_filter_and_punctuation_always_set(self):
        from transcribe_api.stt.batch_client import submit_batch_job

        mock_response = _make_response(
            status_code=201,
            headers={
                "Location": "https://eastus.cognitiveservices.azure.com/speechtotext/transcriptions/xyz"
            },
        )
        captured_payload = {}

        async def capture_post(url, headers, json):
            captured_payload.update(json)
            return mock_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = capture_post

            await submit_batch_job("https://sas-url", "test-job")

        props = captured_payload["properties"]
        assert props["profanityFilterMode"] == "None"
        assert props["punctuationMode"] == "DictatedAndAutomatic"


class TestGetBatchResults:
    @pytest.mark.asyncio
    async def test_returns_dialogue_entries(self):
        from transcribe_api.stt.batch_client import get_batch_results

        files_data = {
            "values": [{"kind": "Transcription", "links": {"contentUrl": "https://results-url"}}]
        }
        result_data = {
            "recognizedPhrases": [
                {
                    "offsetInTicks": 10_000_000,
                    "durationInTicks": 20_000_000,
                    "speaker": 1,
                    "nBest": [{"display": "Hello world", "words": []}],
                }
            ]
        }

        def make_response_for(url, **kwargs):
            if "files" in url:
                return _make_response(body=files_data)
            return _make_response(body=result_data)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=make_response_for)

            entries = await get_batch_results("https://job-url")

        assert len(entries) == 1
        assert entries[0].speaker == "1"
        assert entries[0].start_time == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_captures_word_level_confidence_and_timing(self):
        from transcribe_api.stt.batch_client import get_batch_results

        files_data = {
            "values": [{"kind": "Transcription", "links": {"contentUrl": "https://results-url"}}]
        }
        result_data = {
            "recognizedPhrases": [
                {
                    "offsetInTicks": 0,
                    "durationInTicks": 20_000_000,
                    "speaker": 0,
                    "nBest": [
                        {
                            "display": "Hello world",
                            "confidence": 0.9,
                            "words": [
                                {
                                    "word": "hello",
                                    "offsetInTicks": 0,
                                    "durationInTicks": 5_000_000,
                                    "confidence": 0.95,
                                },
                                {
                                    "word": "world",
                                    "offsetInTicks": 5_000_000,
                                    "durationInTicks": 5_000_000,
                                    "confidence": 0.6,
                                },
                            ],
                        }
                    ],
                }
            ]
        }

        def make_response_for(url, **kwargs):
            if "files" in url:
                return _make_response(body=files_data)
            return _make_response(body=result_data)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=make_response_for)

            entries = await get_batch_results("https://job-url")

        assert entries[0].words is not None
        assert len(entries[0].words) == 2
        assert entries[0].words[0].text == "hello"
        assert entries[0].words[0].start_time == pytest.approx(0.0)
        assert entries[0].words[0].end_time == pytest.approx(0.5)
        assert entries[0].words[1].confidence == pytest.approx(0.6)

    @pytest.mark.asyncio
    async def test_captures_full_nbest_array_as_alternatives(self):
        from transcribe_api.stt.batch_client import get_batch_results

        files_data = {
            "values": [{"kind": "Transcription", "links": {"contentUrl": "https://results-url"}}]
        }
        result_data = {
            "recognizedPhrases": [
                {
                    "offsetInTicks": 0,
                    "durationInTicks": 20_000_000,
                    "speaker": 0,
                    "nBest": [
                        {
                            "display": "Hello world.",
                            "lexical": "hello world",
                            "confidence": 0.5643338,
                            "words": [
                                {
                                    "word": "hello",
                                    "offsetInTicks": 0,
                                    "durationInTicks": 5_000_000,
                                    "confidence": 0.95,
                                },
                                {
                                    "word": "world",
                                    "offsetInTicks": 5_000_000,
                                    "durationInTicks": 5_000_000,
                                    "confidence": 0.6,
                                },
                            ],
                        },
                        {
                            "display": "helloworld",
                            "lexical": "helloworld",
                            "confidence": 0.1769063,
                        },
                        {
                            "display": "hello worlds",
                            "lexical": "hello worlds",
                            "confidence": 0.49964225,
                        },
                    ],
                }
            ]
        }

        def make_response_for(url, **kwargs):
            if "files" in url:
                return _make_response(body=files_data)
            return _make_response(body=result_data)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=make_response_for)

            entries = await get_batch_results("https://job-url")

        assert entries[0].alternatives is not None
        assert len(entries[0].alternatives) == 1
        group = entries[0].alternatives[0]
        # Word-level detail was present, so the group is anchored to the
        # full range of the (2-word) `words` list.
        assert group.start_word_index == 0
        assert group.end_word_index == 1
        assert len(group.candidates) == 3
        assert group.candidates[0].text == "Hello world."
        assert group.candidates[0].confidence == pytest.approx(0.5643338)
        assert group.candidates[0].lexical == "hello world"
        assert group.candidates[1].text == "helloworld"
        assert group.candidates[2].confidence == pytest.approx(0.49964225)

    @pytest.mark.asyncio
    async def test_alternatives_have_no_word_index_when_no_word_detail(self):
        from transcribe_api.stt.batch_client import get_batch_results

        files_data = {
            "values": [{"kind": "Transcription", "links": {"contentUrl": "https://results-url"}}]
        }
        result_data = {
            "recognizedPhrases": [
                {
                    "offsetInTicks": 0,
                    "durationInTicks": 10_000_000,
                    "speaker": 0,
                    "nBest": [
                        {"display": "Hello", "confidence": 0.9},
                        {"display": "Yellow", "confidence": 0.3},
                    ],
                }
            ]
        }

        def make_response_for(url, **kwargs):
            if "files" in url:
                return _make_response(body=files_data)
            return _make_response(body=result_data)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=make_response_for)

            entries = await get_batch_results("https://job-url")

        assert entries[0].words is None
        group = entries[0].alternatives[0]
        assert group.start_word_index is None
        assert group.end_word_index is None
        assert [c.text for c in group.candidates] == ["Hello", "Yellow"]

    @pytest.mark.asyncio
    async def test_single_candidate_nbest_still_recorded_as_one_alternative(self):
        from transcribe_api.stt.batch_client import get_batch_results

        files_data = {
            "values": [{"kind": "Transcription", "links": {"contentUrl": "https://results-url"}}]
        }
        result_data = {
            "recognizedPhrases": [
                {
                    "offsetInTicks": 0,
                    "durationInTicks": 10_000_000,
                    "speaker": 0,
                    "nBest": [{"display": "Hello", "confidence": 0.99}],
                }
            ]
        }

        def make_response_for(url, **kwargs):
            if "files" in url:
                return _make_response(body=files_data)
            return _make_response(body=result_data)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=make_response_for)

            entries = await get_batch_results("https://job-url")

        assert len(entries[0].alternatives[0].candidates) == 1

    @pytest.mark.asyncio
    async def test_words_is_none_when_azure_returns_no_word_detail(self):
        from transcribe_api.stt.batch_client import get_batch_results

        files_data = {
            "values": [{"kind": "Transcription", "links": {"contentUrl": "https://results-url"}}]
        }
        result_data = {
            "recognizedPhrases": [
                {
                    "offsetInTicks": 0,
                    "durationInTicks": 10_000_000,
                    "speaker": 0,
                    "nBest": [{"display": "Hello"}],
                }
            ]
        }

        def make_response_for(url, **kwargs):
            if "files" in url:
                return _make_response(body=files_data)
            return _make_response(body=result_data)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=make_response_for)

            entries = await get_batch_results("https://job-url")

        assert entries[0].words is None

    @pytest.mark.asyncio
    async def test_raises_when_no_transcription_file(self):
        from transcribe_api.stt.batch_client import BatchResultError, get_batch_results

        files_data = {"values": [{"kind": "Report", "links": {}}]}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=_make_response(body=files_data))

            with pytest.raises(BatchResultError):
                await get_batch_results("https://job-url")

    @pytest.mark.asyncio
    async def test_skips_phrase_with_empty_nbest(self):
        from transcribe_api.stt.batch_client import get_batch_results

        files_data = {
            "values": [{"kind": "Transcription", "links": {"contentUrl": "https://results-url"}}]
        }
        result_data = {
            "recognizedPhrases": [
                {"offsetInTicks": 0, "durationInTicks": 0, "speaker": 0, "nBest": []},
                {
                    "offsetInTicks": 10_000_000,
                    "durationInTicks": 5_000_000,
                    "speaker": 1,
                    "nBest": [{"display": "Hello", "words": []}],
                },
            ]
        }

        def make_response_for(url, **kwargs):
            if "files" in url:
                return _make_response(body=files_data)
            return _make_response(body=result_data)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=make_response_for)

            entries = await get_batch_results("https://job-url")

        assert len(entries) == 1


class TestGetModelDetails:
    @pytest.mark.asyncio
    async def test_returns_parsed_json_with_auth_header(self):
        import os

        from transcribe_api.runtime.settings_recording import get_settings
        from transcribe_api.stt.batch_client import get_model_details

        os.environ["AZURE_SPEECH_KEY"] = "super-secret-key"
        get_settings.cache_clear()

        model_url = (
            "https://my-resource.cognitiveservices.azure.com/speechtotext/v3.2/models/base/abc"
        )
        mock_response = _make_response(
            status_code=200,
            body={"displayName": "20240614 Base", "locale": "en-GB"},
        )
        captured = {}

        async def capture_get(url, headers):
            captured["url"] = url
            captured["headers"] = headers
            return mock_response

        try:
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                mock_client.get = capture_get

                result = await get_model_details(model_url)
        finally:
            get_settings.cache_clear()

        assert result == {"displayName": "20240614 Base", "locale": "en-GB"}
        assert captured["url"] == model_url
        # The subscription key travels only in the server-side request header.
        assert captured["headers"]["Ocp-Apim-Subscription-Key"] == "super-secret-key"

    @pytest.mark.asyncio
    async def test_raises_for_status_on_error(self):
        from transcribe_api.stt.batch_client import get_model_details

        mock_response = _make_response(status_code=401)
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("401", request=MagicMock(), response=mock_response)
        )
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)

            with pytest.raises(httpx.HTTPStatusError):
                await get_model_details("https://model-url")


class TestDeleteBatchJob:
    @pytest.mark.asyncio
    async def test_non_fatal_on_404(self):
        from transcribe_api.stt.batch_client import delete_batch_job

        mock_response = _make_response(status_code=404)
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.delete = AsyncMock(return_value=mock_response)

            await delete_batch_job("https://job-url")  # no exception

    @pytest.mark.asyncio
    async def test_succeeds_on_204(self):
        from transcribe_api.stt.batch_client import delete_batch_job

        mock_response = _make_response(status_code=204)
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.delete = AsyncMock(return_value=mock_response)

            await delete_batch_job("https://job-url")


class TestMockSpeechBackend:
    """SPEECH_BACKEND=mock (local dev only, see settings_recording.py).

    Every test here patches httpx.AsyncClient and speech_auth_headers to
    raise if called at all -- not just asserting the right return value, but
    proving the mock path never attempts real credentials or network access.
    """

    @pytest.fixture(autouse=True)
    def mock_backend(self, monkeypatch):
        from transcribe_api.runtime.settings_recording import get_settings

        monkeypatch.setenv("SPEECH_BACKEND", "mock")
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    @staticmethod
    def _forbid_network(monkeypatch):
        def _fail(*args, **kwargs):
            raise AssertionError("mock backend must not touch the network")

        monkeypatch.setattr("httpx.AsyncClient", _fail)
        monkeypatch.setattr("transcribe_api.stt.batch_client.speech_auth_headers", AsyncMock(side_effect=_fail))

    @pytest.mark.asyncio
    async def test_submit_returns_a_mock_job_url(self, monkeypatch):
        from transcribe_api.stt.batch_client import _MOCK_JOB_URL_PREFIX, submit_batch_job

        self._forbid_network(monkeypatch)
        job_url = await submit_batch_job("https://sas-url", "test-job")
        assert job_url.startswith(_MOCK_JOB_URL_PREFIX)

    @pytest.mark.asyncio
    async def test_status_of_a_mock_job_is_succeeded(self, monkeypatch):
        from transcribe_api.stt.batch_client import _MOCK_JOB_URL_PREFIX, get_batch_job_status

        self._forbid_network(monkeypatch)
        status = await get_batch_job_status(f"{_MOCK_JOB_URL_PREFIX}abc-123")
        assert status["status"] == "Succeeded"
        assert status["createdDateTime"] and status["lastActionDateTime"]

    @pytest.mark.asyncio
    async def test_results_of_a_mock_job_are_the_canned_transcript(self, monkeypatch):
        from transcribe_api.stt.batch_client import _MOCK_JOB_URL_PREFIX, get_batch_results

        self._forbid_network(monkeypatch)
        entries = await get_batch_results(f"{_MOCK_JOB_URL_PREFIX}abc-123")
        assert len(entries) == 2
        assert {e.speaker for e in entries} == {"1", "2"}
        assert all(e.text and e.words for e in entries)

    @pytest.mark.asyncio
    async def test_delete_of_a_mock_job_is_a_noop(self, monkeypatch):
        from transcribe_api.stt.batch_client import _MOCK_JOB_URL_PREFIX, delete_batch_job

        self._forbid_network(monkeypatch)
        await delete_batch_job(f"{_MOCK_JOB_URL_PREFIX}abc-123")  # no exception

    # A database can hold real, pending Azure jobs (a non-mock job_url) from
    # before SPEECH_BACKEND was switched to mock, e.g. a developer flipping
    # it on a shared/staging database. These regression-test that such a job
    # falls through to the real Azure path regardless -- SPEECH_BACKEND=mock
    # must never unconditionally complete it with the canned transcript.

    @pytest.mark.asyncio
    async def test_status_of_a_real_job_url_still_calls_azure(self):
        from transcribe_api.stt.batch_client import get_batch_job_status

        mock_response = _make_response(body={"status": "Running"})
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)

            status = await get_batch_job_status("https://eastus.cognitiveservices.azure.com/real-job")

        assert status["status"] == "Running"  # not the mock's "Succeeded"
        mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_results_of_a_real_job_url_still_come_from_azure(self):
        from transcribe_api.stt.batch_client import get_batch_results

        files_data = {"values": [{"kind": "Transcription", "links": {"contentUrl": "https://results-url"}}]}
        result_data = {
            "recognizedPhrases": [
                {
                    "offsetInTicks": 0,
                    "durationInTicks": 10_000_000,
                    "speaker": 3,
                    "nBest": [{"display": "Real Azure text", "words": []}],
                }
            ]
        }

        def make_response_for(url, **kwargs):
            if "files" in url:
                return _make_response(body=files_data)
            return _make_response(body=result_data)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=make_response_for)

            entries = await get_batch_results("https://eastus.cognitiveservices.azure.com/real-job")

        assert [e.text for e in entries] == ["Real Azure text"]

    @pytest.mark.asyncio
    async def test_delete_of_a_real_job_url_still_calls_azure(self):
        from transcribe_api.stt.batch_client import delete_batch_job

        mock_response = _make_response(status_code=204)
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.delete = AsyncMock(return_value=mock_response)

            await delete_batch_job("https://eastus.cognitiveservices.azure.com/real-job")

        mock_client.delete.assert_called_once()


class TestWorkloadIdentityAuth:
    """No Speech key (AKS): every Speech REST call carries an Entra bearer token."""

    @staticmethod
    def _client(mock_client_cls):
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        return mock_client

    @staticmethod
    def _assert_bearer(headers):
        assert headers["Authorization"] == "Bearer aad-token"
        assert "Ocp-Apim-Subscription-Key" not in headers

    @pytest.mark.asyncio
    async def test_submit_sends_bearer_token(self, speech_workload_identity):
        from transcribe_api.stt.batch_client import submit_batch_job

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = self._client(mock_client_cls)
            mock_client.post = AsyncMock(
                return_value=_make_response(status_code=201, headers={"Location": "https://job"})
            )
            await submit_batch_job("https://sas-url", "test-job")

        self._assert_bearer(mock_client.post.call_args.kwargs["headers"])

    @pytest.mark.asyncio
    async def test_status_model_and_delete_send_bearer_token(self, speech_workload_identity):
        from transcribe_api.stt.batch_client import (
            delete_batch_job,
            get_batch_job_status,
            get_model_details,
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = self._client(mock_client_cls)
            mock_client.get = AsyncMock(return_value=_make_response(body={"status": "Running"}))
            mock_client.delete = AsyncMock(return_value=_make_response(status_code=204))

            await get_batch_job_status("https://job")
            await get_model_details("https://test.cognitiveservices.azure.com/models/abc")
            await delete_batch_job("https://job")

        for call in mock_client.get.call_args_list:
            self._assert_bearer(call.kwargs["headers"])
        self._assert_bearer(mock_client.delete.call_args.kwargs["headers"])

    @pytest.mark.asyncio
    async def test_results_listing_sends_bearer_but_sas_content_url_gets_no_auth(
        self, speech_workload_identity
    ):
        from transcribe_api.stt.batch_client import get_batch_results

        files_data = {
            "values": [{"kind": "Transcription", "links": {"contentUrl": "https://results-sas-url"}}]
        }
        calls = []

        async def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/files"):
                return _make_response(body=files_data)
            return _make_response(body={"recognizedPhrases": []})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = self._client(mock_client_cls)
            mock_client.get = fake_get
            await get_batch_results("https://job")

        (files_url, files_kwargs), (content_url, content_kwargs) = calls
        assert files_url == "https://job/files"
        self._assert_bearer(files_kwargs["headers"])
        # The contentUrl is a SAS link: no token is sent to it.
        assert content_url == "https://results-sas-url"
        assert "headers" not in content_kwargs

    @pytest.mark.asyncio
    async def test_credential_failure_is_not_retried_and_makes_no_request(self, speech_workload_identity):
        from transcribe_api.stt.batch_client import submit_batch_job
        from transcribe_api.stt.speech_auth import SpeechAuthError

        speech_workload_identity.error = RuntimeError("no federated token")
        with patch("httpx.AsyncClient") as mock_client_cls:
            with pytest.raises(SpeechAuthError):
                await submit_batch_job("https://sas-url", "test-job")
            mock_client_cls.assert_not_called()
        assert len(speech_workload_identity.scopes) == 1
