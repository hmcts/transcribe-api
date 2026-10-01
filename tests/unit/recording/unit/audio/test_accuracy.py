"""Unit tests for accuracy/needs-review computation."""

from transcribe_api.stt.accuracy import DEFAULT_CONFIDENCE_THRESHOLD, compute_accuracy
from transcribe_api.domain.models_recording import DialogueEntry


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
