from __future__ import annotations

import logging

from transcribe_api.domain.models_recording import DialogueEntry, PhraseAlternatives

logger = logging.getLogger(__name__)


def _merged_alternatives(
    existing: list[PhraseAlternatives] | None,
    existing_word_count: int,
    new: list[PhraseAlternatives] | None,
    words_aligned: bool,
) -> list[PhraseAlternatives] | None:
    """Combine two entries' per-phrase nBest groups when merging speaker turns.

    When both sides still have an intact `words` list (see the words-merge
    rule below), `new`'s word-index ranges are offset by how many words
    already precede it so they keep pointing at the right words in the
    merged list. When alignment breaks (either side lacks `words`), the
    candidates are still kept — just with their index range cleared to
    None — rather than silently dropped, matching how words/confidence
    were previously (and wrongly) dropped entirely in this same module.
    """
    combined = (existing or []) + (new or [])
    if not combined:
        return None
    if words_aligned:
        return (existing or []) + [
            PhraseAlternatives(
                start_word_index=(
                    pa.start_word_index + existing_word_count
                    if pa.start_word_index is not None
                    else None
                ),
                end_word_index=(
                    pa.end_word_index + existing_word_count
                    if pa.end_word_index is not None
                    else None
                ),
                candidates=pa.candidates,
            )
            for pa in (new or [])
        ]
    return [
        PhraseAlternatives(start_word_index=None, end_word_index=None, candidates=pa.candidates)
        for pa in combined
    ]


def _merged_confidence(
    existing_text: str,
    existing_confidence: float | None,
    new_text: str,
    new_confidence: float | None,
) -> float | None:
    """Word-count-weighted average, matching how accuracy.py aggregates confidence.

    Only entries that actually have a confidence value contribute their word
    count to the weighting — an unscored entry must not drag the average
    down as if it scored 0.0.
    """
    existing_words = len(existing_text.split()) if existing_confidence is not None else 0
    new_words = len(new_text.split()) if new_confidence is not None else 0
    total_scored_words = existing_words + new_words
    if total_scored_words == 0:
        return None
    return (
        (existing_confidence or 0.0) * existing_words + (new_confidence or 0.0) * new_words
    ) / total_scored_words


# Azure Speech Batch diarises per recognised phrase, not per sentence. When a
# sentence runs across a phrase boundary, the tail end — often just the last
# few words — is occasionally attributed to a different speaker than the one
# who said the rest of it, splitting a single speaker's turn in two. A
# genuine speaker handover almost always coincides with a pause and lands on
# a sentence boundary, so a raw speaker-id flip that instead has a near-zero
# gap, no sentence-ending punctuation on the text so far, and only a short
# run of new text is treated as a continuation of the current speaker rather
# than a real handover.
_MAX_MID_SENTENCE_GAP_SECONDS = 0.5
# Diarisation timestamps for adjacent phrases can overlap slightly even
# within a single speaker's continuous speech, so a small negative gap is
# tolerated too.
_MIN_MID_SENTENCE_GAP_SECONDS = -0.2
_SENTENCE_TERMINATORS = (".", "!", "?")
# A genuine (different-speaker) turn that happens to follow in quick
# succession is usually a substantial utterance, not a one-or-two-word
# fragment — capping the merge to short fragments limits how often this
# heuristic can accidentally swallow a real speaker change.
_MAX_MISATTRIBUTED_FRAGMENT_WORDS = 8


def _looks_like_mid_sentence_misattribution(
    current_entry: DialogueEntry, entry: DialogueEntry
) -> bool:
    """True if `entry` looks like a mis-diarised continuation of `current_entry`.

    See the module-level comment above for the reasoning behind each check.
    """
    gap = entry.start_time - current_entry.end_time
    if not (_MIN_MID_SENTENCE_GAP_SECONDS <= gap <= _MAX_MID_SENTENCE_GAP_SECONDS):
        return False
    if current_entry.text.rstrip().endswith(_SENTENCE_TERMINATORS):
        return False
    return len(entry.text.split()) <= _MAX_MISATTRIBUTED_FRAGMENT_WORDS


def group_dialogue_entries_by_speaker(
    entries: list[DialogueEntry],
) -> list[DialogueEntry]:
    grouped: list[DialogueEntry] = []
    current_speaker: str | None = None
    current_entry: DialogueEntry | None = None

    for entry in entries:
        is_continuation = current_entry is not None and (
            entry.speaker == current_speaker
            or _looks_like_mid_sentence_misattribution(current_entry, entry)
        )

        if is_continuation and current_entry is not None:
            current_entry.confidence = _merged_confidence(
                current_entry.text, current_entry.confidence, entry.text, entry.confidence
            )
            # Only concatenate if BOTH sides have word-level data — a
            # partial words list (covering just one side) would no longer
            # line up with the merged text's word indices, corrupting
            # word-range corrections and playback-sync highlighting.
            words_aligned = current_entry.words is not None and entry.words is not None
            existing_word_count = len(current_entry.words) if current_entry.words else 0
            current_entry.alternatives = _merged_alternatives(
                current_entry.alternatives, existing_word_count, entry.alternatives, words_aligned
            )
            current_entry.words = current_entry.words + entry.words if words_aligned else None
            current_entry.text += f" {entry.text}"
            # The small negative gap tolerated by the mid-sentence heuristic
            # means `entry` can occasionally overlap and end slightly
            # earlier than the current segment already does — never let a
            # merge shrink the segment's time range.
            current_entry.end_time = max(current_entry.end_time, entry.end_time)
        else:
            if current_entry:
                grouped.append(current_entry)
            # Keep the running speaker label as-is; a mis-attributed
            # fragment (handled above) never reaches this branch, so
            # current_speaker only ever changes here on a genuine handover.
            current_speaker = entry.speaker
            current_entry = DialogueEntry(
                speaker=current_speaker,
                text=entry.text,
                start_time=entry.start_time,
                end_time=entry.end_time,
                confidence=entry.confidence,
                words=entry.words,
                alternatives=entry.alternatives,
            )

    if current_entry:
        grouped.append(current_entry)

    return grouped


def normalize_speaker_labels(entries: list[DialogueEntry]) -> list[DialogueEntry]:
    speaker_map: dict[str, str] = {}
    counter = 0
    result = []

    for entry in entries:
        if entry.speaker not in speaker_map:
            speaker_map[entry.speaker] = str(counter)
            counter += 1
        result.append(
            DialogueEntry(
                speaker=speaker_map[entry.speaker],
                text=entry.text,
                start_time=entry.start_time,
                end_time=entry.end_time,
                confidence=entry.confidence,
                words=entry.words,
                alternatives=entry.alternatives,
            )
        )

    return result


def add_speaker_labels(entries: list[DialogueEntry]) -> list[DialogueEntry]:
    return [
        DialogueEntry(
            speaker=f"Speaker {entry.speaker}",
            text=entry.text,
            start_time=entry.start_time,
            end_time=entry.end_time,
            confidence=entry.confidence,
            words=entry.words,
            alternatives=entry.alternatives,
        )
        for entry in entries
    ]


def process_speakers(entries: list[DialogueEntry]) -> list[DialogueEntry]:
    """Group, normalise, and label speaker entries.

    Each step degrades gracefully: on failure the previous step's output
    is returned rather than raising.
    """
    try:
        grouped = group_dialogue_entries_by_speaker(entries)
    except Exception:
        logger.exception("Speaker grouping failed; returning original entries")
        return entries

    try:
        normalised = normalize_speaker_labels(grouped)
    except Exception:
        logger.exception("Speaker normalisation failed; returning grouped entries")
        return grouped

    try:
        return add_speaker_labels(normalised)
    except Exception:
        logger.exception("Speaker labelling failed; returning normalised entries")
        return normalised
