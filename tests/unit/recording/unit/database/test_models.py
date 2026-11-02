"""Unit tests for DialogueEntry's correction/effective-text logic."""

from transcribe_api.domain.models_recording import (
    CorrectionEntry,
    DialogueEntry,
    NBestCandidate,
    PhraseAlternatives,
    WordCorrection,
    WordInfo,
    merge_dialogue_entries,
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


class TestMergeDialogueEntries:
    def _pair(self, **overrides):
        first = DialogueEntry(
            speaker="Speaker 1",
            text="The quick",
            start_time=1.0,
            end_time=2.0,
            confidence=0.9,
            words=_words("the", "quick"),
        )
        second = DialogueEntry(
            speaker="Speaker 2",
            text="brown fox.",
            start_time=2.5,
            end_time=4.0,
            confidence=0.6,
            words=_words("brown", "fox"),
        )
        return first.model_copy(update=overrides.get("first", {})), second.model_copy(
            update=overrides.get("second", {})
        )

    def test_keeps_first_speaker_and_start_and_second_end(self):
        merged = merge_dialogue_entries(*self._pair())
        assert merged.speaker == "Speaker 1"
        assert merged.start_time == 1.0
        assert merged.end_time == 4.0

    def test_joins_text_with_a_single_space(self):
        first, second = self._pair(first={"text": "The quick "}, second={"text": " brown fox."})
        assert merge_dialogue_entries(first, second).text == "The quick brown fox."

    def test_keeps_the_lower_confidence(self):
        assert merge_dialogue_entries(*self._pair()).confidence == 0.6

    def test_confidence_from_the_only_half_that_has_one(self):
        first, second = self._pair(second={"confidence": None})
        assert merge_dialogue_entries(first, second).confidence == 0.9

    def test_concatenates_words(self):
        merged = merge_dialogue_entries(*self._pair())
        assert [w.text for w in merged.words] == ["the", "quick", "brown", "fox"]

    def test_drops_words_when_one_half_has_none(self):
        first, second = self._pair(second={"words": None})
        assert merge_dialogue_entries(first, second).words is None

    def test_shifts_second_word_corrections_past_first_words(self):
        first, second = self._pair(
            first={"word_corrections": [WordCorrection(start_word_index=0, end_word_index=0, text="a")]},
            second={"word_corrections": [WordCorrection(start_word_index=1, end_word_index=1, text="dog")]},
        )
        merged = merge_dialogue_entries(first, second)
        assert [(wc.start_word_index, wc.end_word_index, wc.text) for wc in merged.word_corrections] == [
            (0, 0, "a"),
            (3, 3, "dog"),
        ]
        assert merged.corrected_text is None
        assert merged.effective_text() == "a quick brown dog"

    def test_flattens_corrections_when_either_half_has_a_whole_segment_correction(self):
        first, second = self._pair(
            first={"word_corrections": [WordCorrection(start_word_index=0, end_word_index=0, text="a")]},
            second={"corrected_text": "red fox."},
        )
        merged = merge_dialogue_entries(first, second)
        assert merged.corrected_text == "a quick red fox."
        assert merged.word_corrections is None

    def test_flattens_word_corrections_when_words_are_dropped(self):
        first, second = self._pair(
            first={"word_corrections": [WordCorrection(start_word_index=0, end_word_index=0, text="a")]},
            second={"words": None},
        )
        merged = merge_dialogue_entries(first, second)
        assert merged.corrected_text == "a quick brown fox."
        assert merged.word_corrections is None

    def test_no_corrections_when_neither_half_was_corrected(self):
        merged = merge_dialogue_entries(*self._pair())
        assert merged.corrected_text is None
        assert merged.word_corrections is None
        assert merged.has_corrections() is False

    def test_history_snapshots_include_the_other_half(self):
        first, second = self._pair(
            first={
                "corrected_text": "A quick",
                "correction_history": [
                    CorrectionEntry(
                        timestamp="2026-01-01T10:00:00+00:00",
                        kind="segment",
                        previous_text="The quick",
                        new_text="A quick",
                    )
                ],
            },
            second={
                "word_corrections": [WordCorrection(start_word_index=1, end_word_index=1, text="dog")],
                "correction_history": [
                    CorrectionEntry(
                        timestamp="2026-01-01T09:00:00+00:00",
                        kind="word_range",
                        previous_text="brown fox.",
                        new_text="brown dog",
                        start_word_index=1,
                        end_word_index=1,
                        previous_phrase="fox",
                        new_phrase="dog",
                    )
                ],
            },
        )
        history = merge_dialogue_entries(first, second).correction_history
        assert [h.kind for h in history] == ["word_range", "segment"]
        assert history[0].previous_text == "A quick brown fox."
        assert history[0].new_text == "A quick brown dog"
        assert (history[0].start_word_index, history[0].end_word_index) == (3, 3)
        assert history[1].previous_text == "The quick brown dog"
        assert history[1].new_text == "A quick brown dog"

    def test_shifts_second_alternatives_and_clears_indices_without_words(self):
        group = PhraseAlternatives(start_word_index=0, end_word_index=1, candidates=[NBestCandidate(text="x")])
        first, second = self._pair(first={"alternatives": [group]}, second={"alternatives": [group]})
        merged = merge_dialogue_entries(first, second)
        assert [(g.start_word_index, g.end_word_index) for g in merged.alternatives] == [(0, 1), (2, 3)]

        first, second = self._pair(first={"alternatives": [group]}, second={"words": None})
        merged = merge_dialogue_entries(first, second)
        assert [(g.start_word_index, g.end_word_index) for g in merged.alternatives] == [(None, None)]

    def test_accepted_only_when_both_halves_were(self):
        first, second = self._pair(first={"accepted": True})
        assert merge_dialogue_entries(first, second).accepted is False
        first, second = self._pair(first={"accepted": True}, second={"accepted": True})
        assert merge_dialogue_entries(first, second).accepted is True

    def test_keeps_word_corrections_when_the_untouched_half_reads_the_same_from_its_words(self):
        first, second = self._pair(
            first={"word_corrections": [WordCorrection(start_word_index=0, end_word_index=0, text="a")]},
            second={"text": "brown fox"},
        )
        merged = merge_dialogue_entries(first, second)
        assert merged.word_corrections is not None
        assert merged.corrected_text is None
        assert merged.effective_text() == "a quick brown fox"

    def test_flattens_when_rebuilding_from_words_would_change_the_untouched_half(self):
        first = DialogueEntry(
            speaker="0",
            text="My favourite colour.",
            start_time=0,
            end_time=1,
            words=_words("my", "favorite", "color"),
        )
        second = DialogueEntry(
            speaker="1",
            text="Is blue.",
            start_time=1,
            end_time=2,
            words=_words("is", "blue"),
            word_corrections=[WordCorrection(start_word_index=1, end_word_index=1, text="green")],
        )
        for a, b, expected in [
            (first, second, "My favourite colour. is green"),
            (second, first, "is green My favourite colour."),
        ]:
            merged = merge_dialogue_entries(a, b)
            assert merged.word_corrections is None
            assert merged.corrected_text == expected
            assert merged.effective_text() == expected


class TestReviewConfidence:
    def test_uncorrected_entry_uses_its_confidence(self):
        entry = DialogueEntry(speaker="0", text="hi", start_time=0, end_time=1, confidence=0.4)
        assert entry.review_confidence() == 0.4

    def test_none_once_accepted(self):
        entry = DialogueEntry(speaker="0", text="hi", start_time=0, end_time=1, confidence=0.4, accepted=True)
        assert entry.review_confidence() is None

    def test_corrected_entry_needs_no_review(self):
        entry = DialogueEntry(speaker="0", text="hi", start_time=0, end_time=1, confidence=0.4, corrected_text="hey")
        assert entry.review_confidence() is None

    def test_merged_entry_uses_unreviewed_confidence_whatever_its_corrections(self):
        entry = DialogueEntry(speaker="0", text="hi", start_time=0, end_time=1, confidence=0.4, merged=True)
        assert entry.review_confidence() is None
        entry.unreviewed_confidence = 0.3
        assert entry.review_confidence() == 0.3
        entry.corrected_text = "hey"
        assert entry.review_confidence() == 0.3


class TestMergeKeepsUnreviewedHalves:
    corrected = DialogueEntry(
        speaker="0", text="The hering", start_time=0, end_time=1, confidence=0.9, corrected_text="The hearing"
    )
    unreviewed = DialogueEntry(speaker="1", text="is adjourned.", start_time=1, end_time=2, confidence=0.4)

    def test_unreviewed_half_still_needs_review_in_both_directions(self):
        for a, b in [(self.corrected, self.unreviewed), (self.unreviewed, self.corrected)]:
            merged = merge_dialogue_entries(a, b)
            assert merged.has_corrections() is True
            assert merged.review_confidence() == 0.4

    def test_accepted_half_is_not_carried(self):
        accepted = self.unreviewed.model_copy(update={"accepted": True})
        assert merge_dialogue_entries(self.corrected, accepted).review_confidence() is None

    def test_accepted_low_half_with_untouched_high_half_needs_no_review_in_both_directions(self):
        accepted = self.unreviewed.model_copy(update={"accepted": True})
        untouched = DialogueEntry(speaker="0", text="Please sit.", start_time=2, end_time=3, confidence=0.9)
        for a, b in [(accepted, untouched), (untouched, accepted)]:
            merged = merge_dialogue_entries(a, b)
            assert merged.confidence == 0.4
            assert merged.review_confidence() == 0.9

    def test_two_untouched_halves_need_review_at_the_lower_confidence(self):
        untouched = DialogueEntry(speaker="0", text="Please sit.", start_time=2, end_time=3, confidence=0.9)
        assert merge_dialogue_entries(untouched, self.unreviewed).review_confidence() == 0.4

    def test_carried_through_a_second_merge(self):
        merged = merge_dialogue_entries(self.corrected, self.unreviewed)
        again = merge_dialogue_entries(self.corrected, merged)
        assert again.review_confidence() == 0.4
