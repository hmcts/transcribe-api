"""Unit tests for DialogueEntry's correction/effective-text logic."""

from transcribe_api.domain.models_recording import (
    DialogueEntry,
    NBestCandidate,
    PhraseAlternatives,
    WordCorrection,
    WordInfo,
)


def _words(*texts: str) -> list[WordInfo]:
    return [
        WordInfo(text=t, start_time=float(i), end_time=float(i) + 1, confidence=0.9)
        for i, t in enumerate(texts)
    ]


class TestHasCorrections:
    def test_false_when_nothing_corrected(self):
        entry = DialogueEntry(speaker="0", text="hello world", start_time=0, end_time=1)
        assert entry.has_corrections() is False

    def test_true_with_whole_segment_correction(self):
        entry = DialogueEntry(
            speaker="0", text="hello world", start_time=0, end_time=1, corrected_text="hi there"
        )
        assert entry.has_corrections() is True

    def test_true_with_a_word_correction(self):
        entry = DialogueEntry(
            speaker="0",
            text="hello world",
            start_time=0,
            end_time=1,
            word_corrections=[WordCorrection(start_word_index=0, end_word_index=0, text="hi")],
        )
        assert entry.has_corrections() is True

    def test_false_when_only_accepted_not_corrected(self):
        """accepted is a distinct concept from has_corrections() — an
        accept-all action confirms the text as-is, it does not correct it."""
        entry = DialogueEntry(
            speaker="0", text="hello world", start_time=0, end_time=1, accepted=True
        )
        assert entry.has_corrections() is False


class TestEffectiveText:
    def test_returns_original_text_when_uncorrected(self):
        entry = DialogueEntry(speaker="0", text="hello world", start_time=0, end_time=1)
        assert entry.effective_text() == "hello world"

    def test_whole_segment_correction_takes_precedence(self):
        entry = DialogueEntry(
            speaker="0",
            text="hello world",
            start_time=0,
            end_time=1,
            corrected_text="hi there",
            word_corrections=[WordCorrection(start_word_index=0, end_word_index=0, text="yo")],
        )
        assert entry.effective_text() == "hi there"

    def test_splices_a_single_word_correction(self):
        entry = DialogueEntry(
            speaker="0",
            text="the quick brown fox",
            start_time=0,
            end_time=1,
            words=_words("the", "quick", "brown", "fox"),
            word_corrections=[WordCorrection(start_word_index=1, end_word_index=1, text="slow")],
        )
        assert entry.effective_text() == "the slow brown fox"

    def test_splices_a_multi_word_range_correction(self):
        entry = DialogueEntry(
            speaker="0",
            text="the quick brown fox",
            start_time=0,
            end_time=1,
            words=_words("the", "quick", "brown", "fox"),
            word_corrections=[
                WordCorrection(start_word_index=1, end_word_index=2, text="very slow")
            ],
        )
        assert entry.effective_text() == "the very slow fox"

    def test_splices_multiple_non_overlapping_corrections(self):
        entry = DialogueEntry(
            speaker="0",
            text="the quick brown fox jumps",
            start_time=0,
            end_time=1,
            words=_words("the", "quick", "brown", "fox", "jumps"),
            word_corrections=[
                WordCorrection(start_word_index=3, end_word_index=3, text="cat"),
                WordCorrection(start_word_index=0, end_word_index=0, text="a"),
            ],
        )
        # order of the list shouldn't matter — sorted by start index internally
        assert entry.effective_text() == "a quick brown cat jumps"

    def test_correction_at_start_of_segment(self):
        entry = DialogueEntry(
            speaker="0",
            text="the quick brown fox",
            start_time=0,
            end_time=1,
            words=_words("the", "quick", "brown", "fox"),
            word_corrections=[WordCorrection(start_word_index=0, end_word_index=0, text="a")],
        )
        assert entry.effective_text() == "a quick brown fox"

    def test_correction_at_end_of_segment(self):
        entry = DialogueEntry(
            speaker="0",
            text="the quick brown fox",
            start_time=0,
            end_time=1,
            words=_words("the", "quick", "brown", "fox"),
            word_corrections=[WordCorrection(start_word_index=3, end_word_index=3, text="cat")],
        )
        assert entry.effective_text() == "the quick brown cat"

    def test_falls_back_to_text_when_word_corrections_present_but_no_words(self):
        entry = DialogueEntry(
            speaker="0",
            text="hello world",
            start_time=0,
            end_time=1,
            word_corrections=[WordCorrection(start_word_index=0, end_word_index=0, text="hi")],
        )
        assert entry.effective_text() == "hello world"


class TestAlternatives:
    """DIAAT-232: Azure's full nBest array, persisted per phrase."""

    def test_defaults_to_none(self):
        entry = DialogueEntry(speaker="0", text="hello world", start_time=0, end_time=1)
        assert entry.alternatives is None

    def test_round_trips_through_model_dump_and_reconstruction(self):
        entry = DialogueEntry(
            speaker="0",
            text="Hello world.",
            start_time=0,
            end_time=1,
            confidence=0.56,
            words=_words("hello", "world"),
            alternatives=[
                PhraseAlternatives(
                    start_word_index=0,
                    end_word_index=1,
                    candidates=[
                        NBestCandidate(text="Hello world.", confidence=0.56, lexical="hello world"),
                        NBestCandidate(text="helloworld", confidence=0.18, lexical="helloworld"),
                    ],
                )
            ],
        )

        dumped = entry.model_dump()
        rebuilt = DialogueEntry(**dumped)

        assert rebuilt.alternatives is not None
        assert len(rebuilt.alternatives) == 1
        group = rebuilt.alternatives[0]
        assert group.start_word_index == 0
        assert group.end_word_index == 1
        assert [c.text for c in group.candidates] == ["Hello world.", "helloworld"]
        assert group.candidates[1].confidence == 0.18
