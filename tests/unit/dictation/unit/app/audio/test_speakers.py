"""Unit tests for pure speaker processing functions."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Block the deep import chain triggered by speakers.py -> llm_calls -> interface_functions -> postgres_database
_mock_llm = MagicMock()
sys.modules.setdefault("transcribe_api.documents.minutes.llm_calls", _mock_llm)

import pytest  # noqa: E402

from transcribe_api.stt.speakers_dictation import (  # noqa: E402
    add_speaker_labels_to_dialogue_entries,
    convert_input_dialogue_entries_to_dialogue_entries,
    group_dialogue_entries_by_speaker,
    normalize_speaker_labels,
    process_speakers_and_dialogue_entries,
)
from transcribe_api.domain.models_dictation import DialogueEntry  # noqa: E402


class TestGroupDialogueEntriesBySpeaker:
    def test_empty_list(self):
        assert group_dialogue_entries_by_speaker([]) == []

    def test_single_entry(self):
        entries = [DialogueEntry(speaker="Guest-1", text="Hello", start_time=0.0, end_time=1.0)]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 1
        assert result[0].speaker == "Guest-1"
        assert result[0].text == "Hello"
        assert result[0].start_time == 0.0
        assert result[0].end_time == 1.0

    def test_same_speaker_throughout_merges_text(self):
        entries = [
            DialogueEntry(speaker="Guest-1", text="Hello", start_time=0.0, end_time=1.0),
            DialogueEntry(speaker="Guest-1", text="world", start_time=1.0, end_time=2.0),
            DialogueEntry(speaker="Guest-1", text="today", start_time=2.0, end_time=3.0),
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 1
        assert result[0].text == "Hello world today"
        assert result[0].start_time == 0.0
        assert result[0].end_time == 3.0

    def test_alternating_speakers_stay_separate(self):
        entries = [
            DialogueEntry(speaker="Guest-1", text="Hello", start_time=0.0, end_time=1.0),
            DialogueEntry(speaker="Guest-2", text="Hi", start_time=1.0, end_time=2.0),
            DialogueEntry(speaker="Guest-1", text="How are you", start_time=2.0, end_time=3.0),
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 3
        assert result[0].speaker == "Guest-1"
        assert result[1].speaker == "Guest-2"
        assert result[2].speaker == "Guest-1"

    def test_consecutive_same_speaker_entries_merged(self):
        entries = [
            DialogueEntry(speaker="Guest-1", text="First", start_time=0.0, end_time=1.0),
            DialogueEntry(speaker="Guest-1", text="second", start_time=1.0, end_time=2.0),
            DialogueEntry(speaker="Guest-2", text="Reply", start_time=2.0, end_time=3.0),
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert len(result) == 2
        assert result[0].text == "First second"
        assert result[0].end_time == 2.0
        assert result[1].text == "Reply"

    def test_start_time_of_group_is_first_entry(self):
        entries = [
            DialogueEntry(speaker="Guest-1", text="A", start_time=5.0, end_time=6.0),
            DialogueEntry(speaker="Guest-1", text="B", start_time=6.0, end_time=7.5),
        ]
        result = group_dialogue_entries_by_speaker(entries)
        assert result[0].start_time == 5.0
        assert result[0].end_time == 7.5


class TestNormalizeSpeakerLabels:
    def test_empty_list(self):
        assert normalize_speaker_labels([]) == []

    def test_single_speaker_assigned_zero(self):
        entries = [DialogueEntry(speaker="Guest-1", text="Hello", start_time=0.0, end_time=1.0)]
        result = normalize_speaker_labels(entries)
        assert result[0].speaker == "0"

    def test_two_speakers_assigned_sequentially(self):
        entries = [
            DialogueEntry(speaker="Guest-1", text="Hello", start_time=0.0, end_time=1.0),
            DialogueEntry(speaker="Guest-2", text="Hi", start_time=1.0, end_time=2.0),
            DialogueEntry(speaker="Guest-1", text="Again", start_time=2.0, end_time=3.0),
        ]
        result = normalize_speaker_labels(entries)
        assert result[0].speaker == "0"
        assert result[1].speaker == "1"
        assert result[2].speaker == "0"

    def test_index_assigned_by_first_appearance(self):
        entries = [
            DialogueEntry(speaker="Charlie", text="Third", start_time=2.0, end_time=3.0),
            DialogueEntry(speaker="Alice", text="First", start_time=0.0, end_time=1.0),
            DialogueEntry(speaker="Bob", text="Second", start_time=1.0, end_time=2.0),
        ]
        result = normalize_speaker_labels(entries)
        assert result[0].speaker == "0"  # Charlie — first seen
        assert result[1].speaker == "1"  # Alice — second
        assert result[2].speaker == "2"  # Bob — third

    def test_text_and_times_preserved(self):
        entries = [DialogueEntry(speaker="Guest-1", text="Original", start_time=1.5, end_time=3.5)]
        result = normalize_speaker_labels(entries)
        assert result[0].text == "Original"
        assert result[0].start_time == 1.5
        assert result[0].end_time == 3.5

    def test_same_speaker_throughout(self):
        entries = [
            DialogueEntry(speaker="Guest-1", text="A", start_time=0.0, end_time=1.0),
            DialogueEntry(speaker="Guest-1", text="B", start_time=1.0, end_time=2.0),
        ]
        result = normalize_speaker_labels(entries)
        assert result[0].speaker == "0"
        assert result[1].speaker == "0"


class TestAddSpeakerLabelsToDialogueEntries:
    def test_empty_list(self):
        assert add_speaker_labels_to_dialogue_entries([]) == []

    def test_adds_speaker_prefix(self):
        entries = [DialogueEntry(speaker="0", text="Hello", start_time=0.0, end_time=1.0)]
        result = add_speaker_labels_to_dialogue_entries(entries)
        assert result[0].speaker == "Speaker 0"

    def test_multiple_speakers_prefixed(self):
        entries = [
            DialogueEntry(speaker="0", text="Hello", start_time=0.0, end_time=1.0),
            DialogueEntry(speaker="1", text="Hi", start_time=1.0, end_time=2.0),
        ]
        result = add_speaker_labels_to_dialogue_entries(entries)
        assert result[0].speaker == "Speaker 0"
        assert result[1].speaker == "Speaker 1"

    def test_text_and_times_preserved(self):
        entries = [DialogueEntry(speaker="2", text="Some text", start_time=5.0, end_time=10.0)]
        result = add_speaker_labels_to_dialogue_entries(entries)
        assert result[0].text == "Some text"
        assert result[0].start_time == 5.0
        assert result[0].end_time == 10.0

    def test_returns_new_objects(self):
        entry = DialogueEntry(speaker="0", text="Hello", start_time=0.0, end_time=1.0)
        result = add_speaker_labels_to_dialogue_entries([entry])
        assert result[0] is not entry


class TestConvertInputDialogueEntriesFromSpeakers:
    """Tests for the convert function defined directly in speakers.py."""

    def test_converts_single_entry(self):
        entries = [{"speaker": "Guest-1", "text": "Hi", "offsetMilliseconds": 500, "durationMilliseconds": 1000}]
        result = convert_input_dialogue_entries_to_dialogue_entries(entries)
        assert len(result) == 1
        assert result[0].speaker == "Guest-1"
        assert result[0].start_time == 0.5
        assert result[0].end_time == 1.5

    def test_empty_list_returns_empty(self):
        assert convert_input_dialogue_entries_to_dialogue_entries([]) == []


@pytest.mark.asyncio
class TestProcessSpeakersAndDialogueEntries:
    @pytest.fixture(autouse=True)
    def _mock_sentry(self):
        with patch("transcribe_api.stt.speakers_dictation.sentry_sdk") as mock:
            yield mock

    @pytest.fixture(autouse=True)
    def _mock_predictions(self, monkeypatch):
        """Ensure generate_speaker_predictions is always an AsyncMock."""
        mock = AsyncMock(return_value={})
        monkeypatch.setattr("transcribe_api.stt.speakers_dictation.generate_speaker_predictions", mock)
        self._mock_predict = mock

    async def test_happy_path_maps_speaker_names(self, monkeypatch):
        monkeypatch.setattr(
            "transcribe_api.stt.speakers_dictation.generate_speaker_predictions",
            AsyncMock(return_value={"Speaker 0": "Judge Smith"}),
        )
        entries = [DialogueEntry(speaker="Guest-1", text="Hello", start_time=0.0, end_time=1.0)]
        result = await process_speakers_and_dialogue_entries(entries, "user@example.com")
        assert len(result) == 1
        assert result[0].speaker == "Judge Smith"

    async def test_empty_predictions_keeps_speaker_label(self):
        entries = [DialogueEntry(speaker="Guest-1", text="A", start_time=0.0, end_time=1.0)]
        result = await process_speakers_and_dialogue_entries(entries, "user@example.com")
        assert len(result) == 1
        assert result[0].speaker == "Speaker 0"

    async def test_returns_entries_when_speaker_prediction_fails(self, monkeypatch):
        monkeypatch.setattr(
            "transcribe_api.stt.speakers_dictation.generate_speaker_predictions",
            AsyncMock(side_effect=RuntimeError("LLM error")),
        )
        entries = [DialogueEntry(speaker="Guest-1", text="Hello", start_time=0.0, end_time=1.0)]
        result = await process_speakers_and_dialogue_entries(entries, "user@example.com")
        assert len(result) == 1

    async def test_empty_entries_returns_empty(self):
        result = await process_speakers_and_dialogue_entries([], "user@example.com")
        assert result == []

    async def test_returns_original_when_group_step_raises(self, monkeypatch):
        """If grouping raises, return the original entries unchanged."""
        from transcribe_api.stt import speakers_dictation as spk_module

        monkeypatch.setattr(spk_module, "group_dialogue_entries_by_speaker", lambda _: (_ for _ in ()).throw(RuntimeError("group error")))
        entries = [DialogueEntry(speaker="G", text="Hi", start_time=0.0, end_time=1.0)]
        result = await process_speakers_and_dialogue_entries(entries, "user@example.com")
        assert result is entries

    async def test_returns_grouped_when_normalize_step_raises(self, monkeypatch):
        """If normalization raises, return the grouped entries."""
        from transcribe_api.stt import speakers_dictation as spk_module

        grouped = [DialogueEntry(speaker="G", text="Hi", start_time=0.0, end_time=1.0)]
        monkeypatch.setattr(spk_module, "group_dialogue_entries_by_speaker", lambda _: grouped)
        monkeypatch.setattr(spk_module, "normalize_speaker_labels", lambda _: (_ for _ in ()).throw(RuntimeError("norm error")))

        entries = [DialogueEntry(speaker="G", text="Hi", start_time=0.0, end_time=1.0)]
        result = await process_speakers_and_dialogue_entries(entries, "user@example.com")
        assert result is grouped

    async def test_returns_normalized_when_label_step_raises(self, monkeypatch):
        """If add_speaker_labels raises, return the normalized entries."""
        from transcribe_api.stt import speakers_dictation as spk_module

        normalized = [DialogueEntry(speaker="0", text="Hi", start_time=0.0, end_time=1.0)]
        monkeypatch.setattr(spk_module, "group_dialogue_entries_by_speaker", lambda e: e)
        monkeypatch.setattr(spk_module, "normalize_speaker_labels", lambda _: normalized)
        monkeypatch.setattr(spk_module, "add_speaker_labels_to_dialogue_entries", lambda _: (_ for _ in ()).throw(RuntimeError("label error")))

        entries = [DialogueEntry(speaker="G", text="Hi", start_time=0.0, end_time=1.0)]
        result = await process_speakers_and_dialogue_entries(entries, "user@example.com")
        assert result is normalized
