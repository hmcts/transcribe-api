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

        from transcribe_api.stt.batch_client import submit_batch_job
        from transcribe_api.runtime.settings_recording import get_settings

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

        from transcribe_api.stt.batch_client import get_model_details
        from transcribe_api.runtime.settings_recording import get_settings

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
