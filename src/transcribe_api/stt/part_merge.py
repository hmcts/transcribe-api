"""Assembling the transcript of a recording that was transcribed in parts (DIAAT-408).

Each part is its own Azure job whose times start at 0. The transcript of the whole recording is the
parts' transcripts in order, each processed as a transcript of its own (speakers are grouped and
numbered per part: diarization is per file, so a speaker of one part is not known to be a speaker of
another) and with every entry and word time moved by the part's ``offset_seconds``: where the part
starts in the recording, as measured from the cut files (transcription_job_part).

Pure functions over the parts' results: no database, no Azure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from transcribe_api.domain.models_recording import DialogueEntry, WordInfo
from transcribe_api.stt.job_telemetry import DURATION_SOURCE_AZURE, DURATION_SOURCE_ESTIMATE, billed_audio_seconds
from transcribe_api.stt.speakers_recording import process_speakers


@dataclass(frozen=True)
class PartResult:
    """One part's results, as Azure returned them."""

    index: int  # 1-based
    count: int
    offset_seconds: float
    duration_seconds: float  # as cut (the part row); what the part's audio is
    entries: list[DialogueEntry]
    azure_duration_seconds: float | None  # what Azure reported for the part's audio, if it did


def offset_entry(entry: DialogueEntry, offset: float) -> DialogueEntry:
    """A copy of ``entry`` with its times (and its words') moved by ``offset`` seconds."""
    words = (
        [
            WordInfo(
                text=w.text,
                start_time=w.start_time + offset,
                end_time=w.end_time + offset,
                confidence=w.confidence,
            )
            for w in entry.words
        ]
        if entry.words is not None
        else None
    )
    return entry.model_copy(
        update={"start_time": entry.start_time + offset, "end_time": entry.end_time + offset, "words": words}
    )


def merge_parts(
    parts: list[PartResult],
    process: Callable[[list[DialogueEntry], PartResult], list[DialogueEntry]] | None = None,
) -> list[DialogueEntry]:
    """The recording's dialogue entries: each part processed on its own (``process``, by default
    ``process_speakers``), offset, and the parts concatenated in order."""
    merged: list[DialogueEntry] = []
    for part in sorted(parts, key=lambda p: p.index):
        processed = process(part.entries, part) if process else process_speakers(part.entries)
        merged.extend(offset_entry(entry, part.offset_seconds) for entry in processed)
    return merged


def billed_seconds(parts: list[PartResult]) -> tuple[float | None, str]:
    """The audio seconds to record as the job's Speech usage: the sum of the parts' durations as Azure
    reported them. A part for which Azure reported none counts at the length it was cut to (and the
    source is then ``estimate``, not ``azure``)."""
    total = 0.0
    source = DURATION_SOURCE_AZURE
    for part in parts:
        seconds, part_source = billed_audio_seconds(part.azure_duration_seconds, part.duration_seconds)
        if seconds is None:
            return None, part_source
        total += seconds
        if part_source != DURATION_SOURCE_AZURE:
            source = DURATION_SOURCE_ESTIMATE
    return total, source
