"""Unit tests for accuracy/needs-review computation."""

import pytest

from transcribe_api.domain.models_recording import (
    DialogueEntry,
    EntryPart,
    WordCorrection,
    WordInfo,
    merge_dialogue_entries,
)
from transcribe_api.stt.accuracy import DEFAULT_CONFIDENCE_THRESHOLD, compute_accuracy


def _entry(
    speaker="0",
    text="hello world",
    start=0.0,
    end=1.0,
    confidence=None,
    corrected=None,
    accepted=False,
):
    return DialogueEntry(
        speaker=speaker,
        text=text,
        start_time=start,
        end_time=end,
        confidence=confidence,
        corrected_text=corrected,
        accepted=accepted,
    )


class TestComputeAccuracy:
    def test_confidence_score_is_word_count_weighted_average(self):
        entries = [
            _entry(text="one two", confidence=1.0),
            _entry(text="three four five six", confidence=0.5),
        ]
        summary = compute_accuracy(entries)
        # (2*1.0 + 4*0.5) / 6 = 4/6 = 0.6667 -> 66.67%
        assert round(summary.confidence_score, 2) == 66.67

    def test_words_transcribed_counts_all_entries(self):
        entries = [_entry(text="one two three"), _entry(text="four five")]
        assert compute_accuracy(entries).words_transcribed == 5

    def test_no_corrections_means_no_wer(self):
        entries = [_entry(confidence=0.9)]
        summary = compute_accuracy(entries)
        assert summary.has_corrections is False
        assert summary.word_error_rate is None
        assert summary.corrected_percent is None

    def test_correction_enables_wer_computation(self):
        entries = [_entry(text="the quick brown fox", corrected="the slow brown fox")]
        summary = compute_accuracy(entries)
        assert summary.has_corrections is True
        assert summary.word_error_rate == 25.0
        assert summary.corrected_percent == 100.0

    def test_partial_corrections_report_correct_percent(self):
        entries = [
            _entry(text="hello world", corrected="hello there"),
            _entry(text="goodbye now"),
        ]
        summary = compute_accuracy(entries)
        assert summary.corrected_percent == 50.0

    def test_low_confidence_segments_appear_in_needs_review(self):
        entries = [
            _entry(speaker="Judge", start=0.0, confidence=0.5),
            _entry(speaker="Counsel", start=5.0, confidence=0.95),
        ]
        summary = compute_accuracy(entries, confidence_threshold=0.85)
        assert summary.low_confidence_count == 1
        assert len(summary.needs_review) == 1
        assert summary.needs_review[0].speaker == "Judge"

    def test_corrected_segments_are_excluded_from_needs_review(self):
        entries = [_entry(confidence=0.5, corrected="fixed text")]
        summary = compute_accuracy(entries, confidence_threshold=0.85)
        assert summary.low_confidence_count == 0
        assert summary.needs_review == []

    def test_entries_with_no_confidence_are_excluded_from_needs_review(self):
        entries = [_entry(confidence=None)]
        summary = compute_accuracy(entries)
        assert summary.needs_review == []

    def test_accepted_segments_are_excluded_from_needs_review(self):
        entries = [_entry(confidence=0.5, accepted=True)]
        summary = compute_accuracy(entries, confidence_threshold=0.85)
        assert summary.low_confidence_count == 0
        assert summary.needs_review == []

    def test_corrected_segment_with_an_unreviewed_merged_part_stays_in_needs_review(self):
        entry = _entry(confidence=0.9, corrected="fixed text")
        entry.merged = True
        entry.unreviewed_confidence = 0.4
        summary = compute_accuracy([entry], confidence_threshold=0.85)
        assert [item.confidence for item in summary.needs_review] == [0.4]

    def test_unreviewed_merged_part_above_threshold_does_not_need_review(self):
        entry = _entry(confidence=0.9, corrected="fixed text")
        entry.merged = True
        entry.unreviewed_confidence = 0.9
        assert compute_accuracy([entry], confidence_threshold=0.85).needs_review == []

    def test_accepting_a_segment_does_not_count_as_a_correction(self):
        """Accept-all is distinct from a real correction: it must not
        contribute to has_corrections()/word_error_rate — there is no
        actual reference text to compare against."""
        entries = [_entry(text="the quick brown fox", confidence=0.5, accepted=True)]
        summary = compute_accuracy(entries, confidence_threshold=0.85)
        assert summary.has_corrections is False
        assert summary.word_error_rate is None
        assert summary.corrected_percent is None

    def test_empty_entries(self):
        summary = compute_accuracy([])
        assert summary.confidence_score == 0.0
        assert summary.words_transcribed == 0
        assert summary.needs_review == []
        assert summary.has_corrections is False

    # DIAAT-235: the default threshold was lowered from 0.85 to reduce how
    # many merely-common-but-correct words get flagged for review.
    def test_default_threshold_is_lowered_to_reduce_review_noise(self):
        assert DEFAULT_CONFIDENCE_THRESHOLD == 0.65

    def test_word_above_new_default_threshold_is_not_flagged(self):
        # 0.75 sits above the new 0.65 default but below the old 0.85 one —
        # this is exactly the "correct common word, imperfect confidence"
        # case the lowered threshold should stop flagging.
        entries = [_entry(speaker="Judge", start=0.0, confidence=0.75)]
        summary = compute_accuracy(entries)
        assert summary.low_confidence_count == 0
        assert summary.needs_review == []

    def test_word_below_new_default_threshold_is_still_flagged(self):
        entries = [_entry(speaker="Judge", start=0.0, confidence=0.5)]
        summary = compute_accuracy(entries)
        assert summary.low_confidence_count == 1
        assert len(summary.needs_review) == 1
        assert summary.needs_review[0].speaker == "Judge"

    def test_no_baseline_means_no_baseline_wer(self):
        entries = [_entry(text="hello world")]
        summary = compute_accuracy(entries)
        assert summary.has_baseline is False
        assert summary.baseline_word_error_rate is None

    def test_baseline_enables_baseline_wer_computation(self):
        entries = [_entry(text="the quick brown fox")]
        summary = compute_accuracy(entries, baseline_transcript="the slow brown fox")
        assert summary.has_baseline is True
        assert summary.baseline_word_error_rate == 25.0

    def test_baseline_wer_ignores_corrections(self):
        # The whole point of a baseline WER is measuring the *original*
        # Speech Batch output against an independent reference — a clerk's
        # correction inside the app should not move this number at all.
        entries = [_entry(text="the quick brown fox", corrected="the slow brown fox")]
        summary = compute_accuracy(entries, baseline_transcript="the quick brown fox")
        assert summary.baseline_word_error_rate == 0.0

    def test_baseline_wer_spans_multiple_entries(self):
        entries = [_entry(text="hello there"), _entry(text="general kenobi")]
        summary = compute_accuracy(entries, baseline_transcript="hello there general kenobi")
        assert summary.baseline_word_error_rate == 0.0

    def test_blank_baseline_is_treated_as_no_baseline(self):
        entries = [_entry(text="hello world")]
        summary = compute_accuracy(entries, baseline_transcript="   ")
        assert summary.has_baseline is False
        assert summary.baseline_word_error_rate is None

    def test_correction_wer_and_baseline_wer_are_independent(self):
        entries = [_entry(text="the quick brown fox", corrected="the quick brown fox")]
        summary = compute_accuracy(entries, baseline_transcript="a completely different sentence")
        # Correction-based WER: corrected text matches original exactly -> 0 error.
        assert summary.word_error_rate == 0.0
        # Baseline WER: original text compared against an unrelated reference.
        assert summary.baseline_word_error_rate == 100.0


class TestAccuracyAcrossMerges:
    def _entries(self):
        return [
            _entry(text="the hering will reconvene", start=0, end=1, confidence=0.7, corrected="the hearing will reconvene"),
            _entry(text="at two o'clock today please", start=1, end=2, confidence=0.95),
            _entry(text="all rise", start=2, end=3, confidence=0.5),
        ]

    def _summary(self, entries):
        return compute_accuracy(entries, confidence_threshold=0.85)

    def test_combining_leaves_score_wer_and_corrected_share_unchanged(self):
        entries = self._entries()
        before = self._summary(entries)
        for i in (0, 1):
            merged = [merge_dialogue_entries(entries[i], entries[i + 1]), *entries[:i], *entries[i + 2 :]]
            after = self._summary(merged)
            assert after.confidence_score == before.confidence_score
            assert after.word_error_rate == before.word_error_rate
            assert after.corrected_percent == before.corrected_percent
            assert after.words_transcribed == before.words_transcribed

    def test_pure_combine_of_uncorrected_segments_has_no_wer(self):
        entries = self._entries()[1:]
        assert self._summary([merge_dialogue_entries(*entries)]).word_error_rate is None

    def test_editing_the_untouched_part_after_a_merge_scores_like_editing_it_separately(self):
        a, b, c = self._entries()
        separate = [a, b.model_copy(update={"corrected_text": "at two o'clock today"}), c]
        merged = merge_dialogue_entries(a, b)
        merged.corrected_text = "the hearing will reconvene at two o'clock today"
        assert self._summary([merged, c]).word_error_rate == self._summary(separate).word_error_rate
        assert self._summary([merged, c]).corrected_percent == self._summary(separate).corrected_percent

    def test_rolling_back_a_merged_segment_clears_its_wer(self):
        merged = merge_dialogue_entries(*self._entries()[:2])
        merged.corrected_text = None
        summary = self._summary([merged])
        assert summary.word_error_rate is None
        assert summary.has_corrections is False

    def test_a_later_edit_does_not_move_an_earlier_correction_between_parts(self):
        sit = _entry(text="please sit", start=0, end=1, confidence=0.9, corrected="please sit down")
        merged = merge_dialogue_entries(sit, _entry(text="thank you", start=1, end=2, confidence=0.9))
        merged.corrected_text = "please sit down thank you all"
        separate = [sit, _entry(text="thank you", start=1, end=2, confidence=0.9, corrected="thank you all")]
        assert self._summary([merged]).word_error_rate == self._summary(separate).word_error_rate
        assert self._summary([merged]).corrected_percent == 100.0

    def test_deleting_a_whole_part_counts_as_errors(self):
        merged = merge_dialogue_entries(*[_entry(text=t, start=i, end=i + 1, confidence=0.9) for i, t in enumerate(["one two", "three four"])])
        merged.corrected_text = "one two"
        assert self._summary([merged]).word_error_rate == 100.0

    def test_a_word_inserted_after_a_part_stays_with_that_part(self):
        sit = _entry(text="please sit", start=0, end=1, confidence=0.9)
        thanks = _entry(text="thank you very much", start=1, end=2, confidence=0.9)
        merged = merge_dialogue_entries(sit, thanks)
        merged.corrected_text = "please sit down thank you very much"
        separate = [sit.model_copy(update={"corrected_text": "please sit down"}), thanks]
        assert self._summary([merged]).word_error_rate == self._summary(separate).word_error_rate

    def test_a_capitalisation_only_correction_scores_the_same_before_and_after_a_merge(self):
        case = _entry(text="hello world", start=0, end=1, confidence=0.9, corrected="Hello world")
        untouched = _entry(text="this is fine", start=1, end=2, confidence=0.9)
        before = self._summary([case, untouched])
        after = self._summary([merge_dialogue_entries(case, untouched)])
        assert before.corrected_percent == 50.0
        assert after.corrected_percent == before.corrected_percent
        assert after.word_error_rate == before.word_error_rate

    def test_merging_two_corrections_one_of_them_capitalisation_only_keeps_both_scored(self):
        first = _entry(text="hello world", start=0, end=1, confidence=0.9, corrected="Hello world")
        second = _entry(text="this iz", start=1, end=2, confidence=0.9, corrected="this is")
        before = self._summary([first, second])
        after = self._summary([merge_dialogue_entries(first, second)])
        assert before.word_error_rate == 25.0
        assert before.corrected_percent == 100.0
        assert after.word_error_rate == 25.0
        assert after.corrected_percent == 100.0

    def test_untouched_parts_stay_out_of_the_score_after_a_merge(self):
        entries = [
            _entry(text="hello world", start=0, end=1, confidence=0.9, corrected="Hello world"),
            _entry(text="this iz", start=1, end=2, confidence=0.9),
            _entry(text="all rise", start=2, end=3, confidence=0.9),
        ]
        merged = merge_dialogue_entries(merge_dialogue_entries(entries[0], entries[1]), entries[2])
        assert [p.corrected for p in merged.parts] == [True, False, False]
        assert self._summary([merged]).corrected_percent == self._summary(entries).corrected_percent
        assert self._summary([merged]).corrected_percent == pytest.approx(100 / 3)

    def test_a_capitalisation_only_edit_after_a_merge_counts_for_that_part(self):
        a = _entry(text="hello world", start=0, end=1, confidence=0.9)
        b = _entry(text="this is fine", start=1, end=2, confidence=0.9)
        merged = merge_dialogue_entries(a, b)
        merged.corrected_text = "Hello world this is fine"
        separate = [a.model_copy(update={"corrected_text": "Hello world"}), b]
        assert self._summary([merged]).corrected_percent == self._summary(separate).corrected_percent == 50.0
        assert self._summary([merged]).word_error_rate == self._summary(separate).word_error_rate

    def test_the_correction_state_survives_a_second_merge(self):
        a = _entry(text="hello world", start=0, end=1, confidence=0.9, corrected="Hello world")
        b = _entry(text="this is fine", start=1, end=2, confidence=0.9)
        c = _entry(text="all rise", start=2, end=3, confidence=0.9)
        merged = merge_dialogue_entries(merge_dialogue_entries(a, b), c)
        assert self._summary([merged]).corrected_percent == self._summary([a, b, c]).corrected_percent

    def test_parts_stored_without_a_correction_state_are_judged_by_their_wording_as_before(self):
        legacy = DialogueEntry(
            speaker="0",
            text="hello world this iz",
            start_time=0,
            end_time=2,
            confidence=0.9,
            corrected_text="Hello world this is",
            merged=True,
            parts=[
                EntryPart(text="hello world", current_text="Hello world", confidence=0.9),
                EntryPart(text="this iz", current_text="this is", confidence=0.9),
            ],
        )
        assert all(p.corrected is None for p in legacy.parts)
        summary = self._summary([legacy])
        # Only the part whose words changed is scored, exactly as before the state existed.
        assert summary.corrected_percent == 50.0
        assert summary.word_error_rate == 50.0

    def test_legacy_parts_round_trip_without_gaining_a_correction_state(self):
        part = EntryPart.model_validate({"text": "a b", "current_text": "a b", "confidence": 0.5})
        assert part.corrected is None

    def _rolled_back_then_merged(self, *, hard):
        a = _entry(text="hello world", start=0, end=1, confidence=0.9, corrected="Hello world")
        b = _entry(text="this is fine", start=1, end=2, confidence=0.9)
        c = _entry(text="all rize", start=2, end=3, confidence=0.9, corrected="all rise")
        merged = merge_dialogue_entries(a, b)
        merged.corrected_text = None
        if hard:  # what rollback_segment does
            merged.parts = [p.model_copy(update={"current_text": p.text, "corrected": False}) for p in merged.parts]
        never_corrected = [a.model_copy(update={"corrected_text": None}), b, c]
        return merge_dialogue_entries(merged, c), never_corrected

    @pytest.mark.parametrize("hard", [True, False])
    def test_a_rolled_back_merged_part_is_not_counted_again_when_merged_with_a_corrected_segment(self, hard):
        again, never_corrected = self._rolled_back_then_merged(hard=hard)
        assert [p.corrected for p in again.parts] == [False, False, True]
        assert self._summary([again]).word_error_rate == self._summary(never_corrected).word_error_rate
        assert self._summary([again]).corrected_percent == self._summary(never_corrected).corrected_percent
        assert self._summary([again]).corrected_percent == pytest.approx(100 / 3)

    def test_editing_a_part_again_after_a_rollback_counts_it_again(self):
        again, _ = self._rolled_back_then_merged(hard=True)
        again.corrected_text = "Hello world this is fine all rise"
        assert [p.corrected for p in again.current_parts()] == [True, False, True]
        assert self._summary([again]).corrected_percent == pytest.approx(200 / 3)

    def test_a_rollback_leaves_legacy_parts_without_a_correction_state(self):
        part = EntryPart(text="a b", current_text="A b", confidence=0.5)
        merged = DialogueEntry(speaker="0", text="a b c", start_time=0, end_time=1, merged=True, parts=[part, EntryPart(text="c", current_text="c")])
        merged.corrected_text = "a b c"
        assert [p.corrected for p in merged.current_parts()] == [None, None]

    @staticmethod
    def _with_words(text, words, start):
        entry = _entry(text=text, start=start, end=start + 1, confidence=0.9)
        entry.words = [WordInfo(text=w, start_time=start, end_time=start + 1, confidence=0.9) for w in words.split()]
        return entry

    def test_a_word_range_edit_after_a_merge_leaves_an_untouched_part_out_of_the_score(self):
        hello = self._with_words("Hello world", "hello world", 0)
        iz = self._with_words("this iz", "this iz", 1)
        merged = merge_dialogue_entries(hello, iz)
        merged.word_corrections = [WordCorrection(start_word_index=3, end_word_index=3, text="is")]
        separate = [hello, iz.model_copy(update={"corrected_text": "this is"})]
        assert [p.corrected for p in merged.current_parts()] == [False, True]
        assert self._summary([merged]).corrected_percent == self._summary(separate).corrected_percent == 50.0
        assert self._summary([merged]).word_error_rate == self._summary(separate).word_error_rate

    def test_editing_one_part_keeps_another_parts_correction_that_matches_its_asr_text(self):
        same = _entry(text="hello world", start=0, end=1, confidence=0.9, corrected="hello world")
        iz = _entry(text="this iz", start=1, end=2, confidence=0.9, corrected="this is")
        merged = merge_dialogue_entries(same, iz)
        merged.corrected_text = "hello world this was"
        separate = [same, iz.model_copy(update={"corrected_text": "this was"})]
        assert [p.corrected for p in merged.current_parts()] == [True, True]
        assert self._summary([merged]).corrected_percent == self._summary(separate).corrected_percent == 100.0
        assert self._summary([merged]).word_error_rate == self._summary(separate).word_error_rate

    @pytest.mark.parametrize("replacement", ["Hello", "hello"])
    def test_a_word_range_edit_that_keeps_the_text_counts_the_same_before_and_after_a_merge(self, replacement):
        hello = self._with_words("Hello world", "hello world", 0)
        iz = self._with_words("this iz", "this iz", 1)
        edit = [WordCorrection(start_word_index=0, end_word_index=0, text=replacement)]
        merged = merge_dialogue_entries(hello, iz)
        merged.word_corrections = edit
        separate = [hello.model_copy(update={"word_corrections": edit}), iz]
        assert [p.corrected for p in merged.current_parts()] == [True, False]
        assert self._summary([merged]).corrected_percent == self._summary(separate).corrected_percent == 50.0

    def test_a_history_rollback_to_the_asr_text_clears_every_parts_correction_state(self):
        same = _entry(text="hello world", start=0, end=1, confidence=0.9, corrected="hello world")
        iz = _entry(text="this iz", start=1, end=2, confidence=0.9, corrected="this is")
        rize = _entry(text="all rize", start=2, end=3, confidence=0.9, corrected="all rise")
        merged = merge_dialogue_entries(same, iz)
        merged.corrected_text = None
        assert [p.corrected for p in merged.current_parts()] == [False, False]
        again = merge_dialogue_entries(merged, rize)
        never_corrected = [
            same.model_copy(update={"corrected_text": None}),
            iz.model_copy(update={"corrected_text": None}),
            rize,
        ]
        assert [p.corrected for p in again.parts] == [False, False, True]
        assert self._summary([again]).corrected_percent == self._summary(never_corrected).corrected_percent
        assert self._summary([again]).word_error_rate == self._summary(never_corrected).word_error_rate

    def test_a_part_emptied_by_a_word_range_edit_stays_corrected_after_another_merge(self):
        one, two, three = (self._with_words(t, t, i) for i, t in enumerate(["one", "two", "three"]))
        merged = merge_dialogue_entries(one, two)
        merged.word_corrections = [WordCorrection(start_word_index=0, end_word_index=1, text="one")]
        again = merge_dialogue_entries(merged, three)
        separate = [
            one.model_copy(update={"corrected_text": "one"}),
            two.model_copy(update={"corrected_text": ""}),
            three,
        ]
        assert [p.corrected for p in again.current_parts()] == [True, True, False]
        assert self._summary([again]).corrected_percent == pytest.approx(200 / 3)
        assert self._summary([again]).corrected_percent == self._summary(separate).corrected_percent
        assert self._summary([again]).word_error_rate == self._summary(separate).word_error_rate

    def test_re_editing_a_part_emptied_by_a_word_range_edit_scores_it_as_its_own_text(self):
        one, two, three = (self._with_words(t, t, i) for i, t in enumerate(["one", "two", "three"]))
        merged = merge_dialogue_entries(one, two)
        merged.word_corrections = [WordCorrection(start_word_index=0, end_word_index=1, text="one")]
        again = merge_dialogue_entries(merged, three)
        again.word_corrections = [WordCorrection(start_word_index=1, end_word_index=1, text="two new")]
        separate = [one, two.model_copy(update={"corrected_text": "two new"}), three]
        parts = again.current_parts()
        assert [p.current_text for p in parts] == ["one", "two new", "three"]
        assert [p.corrected for p in parts] == [False, True, False]
        assert self._summary([again]).word_error_rate == self._summary(separate).word_error_rate == 50.0
        assert self._summary([again]).corrected_percent == self._summary(separate).corrected_percent

    @pytest.mark.parametrize(("replacement", "wer"), [("one two three four", 0.0), ("one two three for", 25.0)])
    def test_a_word_range_edit_across_merged_parts_is_split_between_them(self, replacement, wer):
        first = self._with_words("one two", "one two", 0)
        second = self._with_words("three four", "three four", 1)
        merged = merge_dialogue_entries(first, second)
        merged.word_corrections = [WordCorrection(start_word_index=0, end_word_index=3, text=replacement)]
        parts = merged.current_parts()
        assert [p.current_text for p in parts] == ["one two", replacement.removeprefix("one two ")]
        assert [p.corrected for p in parts] == [True, True]
        assert self._summary([merged]).word_error_rate == wer

    def test_a_word_range_edit_counts_for_a_new_part_merged_with_legacy_parts(self):
        legacy = DialogueEntry(
            speaker="0",
            text="one two",
            start_time=0,
            end_time=2,
            confidence=0.9,
            words=[WordInfo(text=w, start_time=0, end_time=2, confidence=0.9) for w in ["one", "two"]],
            merged=True,
            parts=[
                EntryPart(text="one", current_text="one", confidence=0.9),
                EntryPart(text="two", current_text="two", confidence=0.9),
            ],
        )
        merged = merge_dialogue_entries(legacy, self._with_words("Three", "three", 2))
        merged.word_corrections = [WordCorrection(start_word_index=2, end_word_index=2, text="Three")]
        assert [p.corrected for p in merged.current_parts()] == [None, None, True]
        assert self._summary([merged]).corrected_percent == pytest.approx(100 / 3)

    def test_merging_legacy_parts_with_a_new_segment_leaves_their_score_unchanged(self):
        legacy = DialogueEntry(
            speaker="0",
            text="one two three",
            start_time=0,
            end_time=3,
            confidence=0.9,
            words=[WordInfo(text=w, start_time=0, end_time=3, confidence=0.9) for w in ["one", "two", "three"]],
            word_corrections=[WordCorrection(start_word_index=1, end_word_index=1, text="two new")],
            merged=True,
            parts=[
                EntryPart(text="one", current_text="one two new", confidence=0.9),
                EntryPart(text="two", current_text="", confidence=0.9),
                EntryPart(text="three", current_text="three", confidence=0.9),
            ],
        )
        four = self._with_words("four", "four", 3)
        merged = merge_dialogue_entries(legacy, four)
        assert merged.word_corrections
        assert self._summary([merged]).word_error_rate == self._summary([legacy, four]).word_error_rate
        assert self._summary([merged]).corrected_percent == self._summary([legacy, four]).corrected_percent
