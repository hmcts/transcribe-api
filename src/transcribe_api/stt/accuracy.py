"""Accuracy/needs-review computation for a completed transcription job.

Three distinct things get surfaced to the frontend, and they must not be
conflated:
- Confidence: Azure's own per-phrase confidence score. Always available,
  but not a measurement of correctness — nothing has verified it against
  ground truth.
- Correction-based word error rate: only meaningful once a clerk has
  corrected at least one segment, since that's the first point a real
  reference transcript exists to measure the original auto-generated
  wording against (see wer.py). Restricted to corrected segments only, so
  it's necessarily a partial picture — segments nobody has looked at yet
  contribute nothing to it.
- Baseline word error rate: computed against an independent reference
  transcript the clerk uploads separately (e.g. a court reporter's
  transcript), compared against the *entire* auto-generated transcription.
  Unlike the correction-based WER, it covers the whole transcript from the
  moment a baseline is uploaded and is completely unaffected by any
  corrections made in this app — the two numbers can disagree, and that's
  expected rather than a bug.
"""

from __future__ import annotations

from dataclasses import dataclass

from transcribe_api.domain.models_recording import DialogueEntry, EntryPart
from transcribe_api.stt.wer import aggregate_word_error_rate, baseline_word_error_rate

# Azure's per-word confidence often sits in the high 70s/low 80s for
# correctly-recognised but short/common words (e.g. "the", "this") purely
# because of acoustic/language-model uncertainty in that instant, not because
# the word is wrong. At 0.85 that noise dominates the needs-review list and
# overwhelms reviewers. Genuinely uncertain recognitions (unclear audio,
# real mishears) tend to fall well below that, so 0.65 keeps flagging
# meaningful for review while cutting out the common-word noise (see
# DIAAT-235).
DEFAULT_CONFIDENCE_THRESHOLD = 0.65


@dataclass(frozen=True)
class NeedsReviewItem:
    speaker: str
    start_time: float
    confidence: float


@dataclass(frozen=True)
class AccuracySummary:
    confidence_score: float  # 0-100, word-count-weighted average
    words_transcribed: int
    low_confidence_count: int
    confidence_threshold: float  # 0-100
    has_corrections: bool
    word_error_rate: float | None  # 0-100+, only set once a segment is corrected
    corrected_percent: float | None  # % of segments corrected so far
    needs_review: list[NeedsReviewItem]
    has_baseline: bool  # True once a clerk has uploaded a reference transcript
    # 0-100+, WER of the whole auto-generated transcription against the
    # uploaded baseline — independent of any in-app corrections. Only set
    # once a baseline has been uploaded.
    baseline_word_error_rate: float | None


def _original_parts(entry: DialogueEntry) -> list[EntryPart]:
    return entry.parts or [EntryPart(text=entry.text, current_text=entry.text, confidence=entry.confidence)]


def _corrected_parts(entry: DialogueEntry) -> list[EntryPart]:
    """The original segments in an entry that have a clerk's text to measure against.

    Scored per original segment, so that merging a corrected segment with an
    untouched one doesn't count the untouched words as corrected. Whether a
    segment was corrected is its recorded state, so a correction that only
    changes capitalisation counts as it does for a segment that was never merged.
    """
    if not entry.has_corrections():
        return []
    parts = entry.current_parts()
    if not entry.parts:
        return parts
    return [p for p in parts if _is_corrected(p)]


def _is_corrected(part: EntryPart) -> bool:
    if part.corrected is not None:
        return part.corrected
    # Parts stored before `corrected` existed: judged by their wording, as they always were.
    return part.current_text.lower().split() != part.text.lower().split()


def compute_accuracy(
    entries: list[DialogueEntry],
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    baseline_transcript: str | None = None,
) -> AccuracySummary:
    parts = [p for e in entries for p in _original_parts(e)]
    scored = [p for p in parts if p.confidence is not None]
    total_words = sum(len(e.text.split()) for e in entries)

    weighted_words = sum(len(p.text.split()) for p in scored)
    confidence_score = (
        sum(p.confidence * len(p.text.split()) for p in scored) / weighted_words
        if weighted_words
        else 0.0
    )

    has_corrections = any(e.has_corrections() for e in entries)

    needs_review = [
        NeedsReviewItem(speaker=e.speaker, start_time=e.start_time, confidence=confidence)
        for e in entries
        if (confidence := e.review_confidence()) is not None and confidence < confidence_threshold
    ]

    wer = None
    corrected_percent = None
    if has_corrections:
        # Only segments a clerk has actually corrected have a real
        # reference to measure against — restricting to those keeps the
        # edit-distance computation cheap (short segments, not the whole
        # transcript) and the metric honest (uncorrected segments would
        # otherwise contribute zero error and dilute the result).
        corrected = [p for e in entries for p in _corrected_parts(e)]
        pairs = [(p.current_text, p.text) for p in corrected]
        wer = aggregate_word_error_rate(pairs) * 100
        corrected_percent = (len(corrected) / len(parts)) * 100 if parts else 0.0

    has_baseline = bool(baseline_transcript and baseline_transcript.strip())
    baseline_wer = None
    if has_baseline:
        # The *original* auto-generated wording, not effective_text() — the
        # whole point of a baseline WER is measuring Speech Batch's own
        # output independent of any corrections a clerk has since made.
        full_original_text = " ".join(e.text for e in entries)
        baseline_wer = baseline_word_error_rate(baseline_transcript, full_original_text) * 100

    return AccuracySummary(
        confidence_score=confidence_score * 100,
        words_transcribed=total_words,
        low_confidence_count=len(needs_review),
        confidence_threshold=confidence_threshold * 100,
        has_corrections=has_corrections,
        word_error_rate=wer,
        corrected_percent=corrected_percent,
        needs_review=needs_review,
        has_baseline=has_baseline,
        baseline_word_error_rate=baseline_wer,
    )
