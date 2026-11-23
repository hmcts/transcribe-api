"""Splitting a long recording into parts Azure Speech can take (DIAAT-408).

Azure batch transcription with diarization fails a file longer than 240 minutes
(``AZURE_BATCH_FILE_LIMIT_SECONDS``), so a longer recording is cut into parts, each transcribed as
its own job. Three steps, kept apart so the middle one can be tested exhaustively:

1. ``detect_silences``: ffmpeg's ``silencedetect`` over the 16 kHz mono WAV (a read pass; its
   stderr is parsed by ``parse_silencedetect``).
2. ``plan_parts``: a **pure** function. From the duration and the silences it chooses the cuts:
   ``n = ceil(duration / target)`` parts of equal length aimed for, each cut moved to the longest
   silence in a window around its ideal point, or made right at the ideal point when the window
   holds none (``cut_kind`` says which). Whatever the input, the plan is checked
   (``check_plan``): parts are contiguous, cover [0, duration], and none is longer than the limit.
   A recording no longer than the limit is a single part.
3. ``cut_segments``: ``ffmpeg -c copy -f segment`` on the WAV: no re-encode, close to disk speed.
   The segment muxer cuts at packet boundaries of the PCM data (about 0.06 s), so the **actual**
   length and offset of each part are measured from the files, not taken from the plan; the sum of
   the parts is still the whole recording to within a sample or two.

Why the planner works to ``limit - SEGMENT_SLACK_SECONDS``: the cut can land up to a packet after
the planned point, and a part must still be within Azure's limit when it has.
"""

from __future__ import annotations

import asyncio
import math
import re
import wave
from dataclasses import dataclass
from pathlib import Path

from transcribe_api.runtime.settings_recording import AZURE_BATCH_FILE_LIMIT_SECONDS
from transcribe_api.stt.subprocesses import run_process

# How far from a part's ideal end a cut may move to reach a silence. Never more than a quarter of
# the parts' spacing (plan_parts), so the cuts stay in order whatever the target.
CUT_WINDOW_SECONDS = 600.0
# What counts as silence: below this level for at least this long.
SILENCE_NOISE_DB = -35.0
SILENCE_MIN_SECONDS = 0.5
# The segment muxer cuts at packet boundaries, up to about this far after the planned point.
SEGMENT_SLACK_SECONDS = 1.0
# The shortest part a plan may contain (a part of a fraction of a second is not transcribable).
MIN_PART_SECONDS = 1.0

CUT_START = "start"  # the first part: not a cut
CUT_SILENCE = "silence"  # cut inside a silence
CUT_FORCED = "forced"  # no silence in the window: cut at the ideal point


class SplitError(Exception):
    """The recording could not be split (ffmpeg failed, or the result broke an invariant)."""


@dataclass(frozen=True)
class Silence:
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class PlannedPart:
    """One part of the plan. ``cut_kind`` says how its start was chosen."""

    index: int  # 1-based
    start: float
    end: float
    cut_kind: str

    @property
    def duration(self) -> float:
        return self.end - self.start


# 1. Silence detection ----------------------------------------------------------------------

_SILENCE_START = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[0-9.]+)")


def parse_silencedetect(stderr: str, duration: float) -> list[Silence]:
    """The silences in ffmpeg's ``silencedetect`` log, in order, clipped to [0, duration].

    A ``silence_start`` with no ``silence_end`` (the file ends in silence) runs to ``duration``.
    ``silence_start`` can be slightly negative at the very start of a file: it is clipped to 0.
    """
    silences: list[Silence] = []
    open_at: float | None = None
    for line in stderr.splitlines():
        started = _SILENCE_START.search(line)
        if started:
            open_at = float(started.group(1))
            continue
        ended = _SILENCE_END.search(line)
        if ended and open_at is not None:
            start, end = max(0.0, open_at), min(duration, float(ended.group(1)))
            if end > start:
                silences.append(Silence(start, end))
            open_at = None
    if open_at is not None and duration > max(0.0, open_at):
        silences.append(Silence(max(0.0, open_at), duration))
    return silences


async def detect_silences(
    wav_path: Path,
    duration: float,
    *,
    noise_db: float = SILENCE_NOISE_DB,
    min_seconds: float = SILENCE_MIN_SECONDS,
) -> list[Silence]:
    """Run ``silencedetect`` over a WAV (a read pass, no output). Raises SplitError if ffmpeg fails."""
    result = await run_process(
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-i",
        str(wav_path),
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_seconds}",
        "-f",
        "null",
        "-",
    )
    text = result.stderr.decode(errors="replace")
    if result.returncode != 0:
        raise SplitError(f"ffmpeg silencedetect failed (exit {result.returncode}): {text[-300:]}")
    return parse_silencedetect(text, duration)


# 2. The plan -------------------------------------------------------------------------------


def _best_cut(silences: list[Silence], low: float, high: float, ideal: float) -> tuple[float, str]:
    """The cut for one boundary within [low, high]: the middle of the longest silence in it (the
    part of a silence inside the window; ties go to the one nearest ``ideal``), or ``ideal`` itself
    (made to fit [low, high]) if there is none."""
    best: tuple[float, float, float] | None = None  # (inside length, -distance, cut)
    for silence in silences:
        start, end = max(silence.start, low), min(silence.end, high)
        if end <= start:
            continue
        cut = (start + end) / 2
        candidate = (end - start, -abs(cut - ideal), cut)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is not None:
        return best[2], CUT_SILENCE
    return min(max(ideal, low), high), CUT_FORCED


def plan_parts(
    duration: float,
    silences: list[Silence],
    *,
    target: float,
    limit: float = AZURE_BATCH_FILE_LIMIT_SECONDS,
    window: float = CUT_WINDOW_SECONDS,
) -> list[PlannedPart]:
    """Plan the parts of a recording ``duration`` seconds long. Pure: no files, no clock.

    A recording of at most ``limit`` seconds is one part. Otherwise there are
    ``n = ceil(duration / min(target, limit - slack))`` parts, aimed at ``duration / n`` each; each cut
    may move up to ``window`` seconds (at most a quarter of that spacing) to the longest silence there.
    Raises SplitError if the plan cannot satisfy the invariants (``check_plan``).
    """
    if not math.isfinite(duration) or duration <= 0:
        raise SplitError(f"cannot split a recording of {duration} seconds")
    if limit <= SEGMENT_SLACK_SECONDS + MIN_PART_SECONDS or target <= 0:
        raise SplitError("the part limit and target must be positive and larger than the cutting slack")
    if duration <= limit:
        return check_plan([PlannedPart(1, 0.0, duration, CUT_START)], duration, limit)

    usable = limit - SEGMENT_SLACK_SECONDS  # a cut can land a packet late: stay this far inside the limit
    count = math.ceil(duration / min(target, usable))
    spacing = duration / count
    reach = min(window, spacing / 4)
    ordered = sorted(silences, key=lambda s: s.start)

    cuts: list[tuple[float, str]] = []
    previous = 0.0
    for k in range(1, count):
        ideal = k * spacing
        # Within the window, and so that this part and every part after it can still fit the limit.
        low = max(ideal - reach, duration - usable * (count - k))
        high = min(ideal + reach, previous + usable)
        cut, kind = _best_cut(ordered, low, high, ideal)
        cuts.append((cut, kind))
        previous = cut

    bounds = [(0.0, CUT_START), *cuts, (duration, "")]
    parts = [PlannedPart(i + 1, bounds[i][0], bounds[i + 1][0], bounds[i][1]) for i in range(count)]
    return check_plan(parts, duration, limit)


def check_plan(parts: list[PlannedPart], duration: float, limit: float) -> list[PlannedPart]:
    """Assert the plan's invariants, loudly: contiguous, covering [0, duration] exactly, every part
    positive and at most ``limit`` seconds, indices 1..n, the first part's start not a cut. Returns
    the plan."""
    if not parts:
        raise SplitError("empty plan")
    if parts[0].start != 0.0 or parts[-1].end != duration:
        raise SplitError("the plan does not cover the recording")
    for i, part in enumerate(parts):
        if part.index != i + 1:
            raise SplitError("part indices are not 1..n")
        if not part.end > part.start:
            raise SplitError(f"part {part.index} is empty or reversed")
        if part.duration > limit:
            raise SplitError(f"part {part.index} is {part.duration:.1f} s, over the limit of {limit:.0f} s")
        if i and part.start != parts[i - 1].end:
            raise SplitError(f"part {part.index} does not start where part {i} ends")
        if (part.cut_kind == CUT_START) != (i == 0):
            raise SplitError(f"part {part.index} has the wrong cut kind")
    return parts


# 3. Cutting --------------------------------------------------------------------------------


@dataclass(frozen=True)
class CutPart:
    """A part as cut: its file, and its offset and length as measured from the files."""

    index: int
    path: Path
    offset_seconds: float
    duration_seconds: float


def _wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wav:
            return wav.getnframes() / wav.getframerate()
    except Exception as exc:
        raise SplitError(f"a part could not be read back ({type(exc).__name__})") from exc


async def cut_segments(
    wav_path: Path,
    cut_times: list[float],
    out_dir: Path,
    *,
    total_seconds: float,
    limit: float = AZURE_BATCH_FILE_LIMIT_SECONDS,
) -> list[CutPart]:
    """Cut ``wav_path`` at ``cut_times`` into ``out_dir/part-001.wav`` ... with
    ``ffmpeg -c copy -f segment`` (no re-encode). Returns the parts with their measured offsets and
    lengths, after checking that there are ``len(cut_times) + 1`` of them, that none is longer than
    ``limit``, and that together they are the whole recording (to within a hundredth of a second).
    Raises SplitError otherwise."""
    pattern = out_dir / "part-%03d.wav"
    result = await run_process(
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(wav_path),
        "-f",
        "segment",
        "-segment_times",
        ",".join(f"{t:.6f}" for t in cut_times),
        "-c",
        "copy",
        "-segment_format",
        "wav",
        "-reset_timestamps",
        "1",
        "-y",
        str(pattern),
    )
    if result.returncode != 0:
        raise SplitError(
            f"ffmpeg segment failed (exit {result.returncode}): {result.stderr.decode(errors='replace')[-300:]}"
        )

    files = sorted(out_dir.glob("part-*.wav"))
    if len(files) != len(cut_times) + 1:
        raise SplitError(f"expected {len(cut_times) + 1} parts, ffmpeg wrote {len(files)}")
    parts: list[CutPart] = []
    offset = 0.0
    for i, path in enumerate(files):
        length = await asyncio.to_thread(_wav_seconds, path)
        if length > limit:
            raise SplitError(f"part {i + 1} came out {length:.1f} s long, over the limit of {limit:.0f} s")
        parts.append(CutPart(i + 1, path, offset, length))
        offset += length
    if abs(offset - total_seconds) > 0.01:
        raise SplitError(f"the parts add up to {offset:.3f} s, the recording is {total_seconds:.3f} s")
    return parts


async def split_wav(
    wav_path: Path,
    duration: float,
    out_dir: Path,
    *,
    target: float,
    limit: float = AZURE_BATCH_FILE_LIMIT_SECONDS,
    window: float = CUT_WINDOW_SECONDS,
) -> tuple[list[PlannedPart], list[CutPart]]:
    """Detect silences, plan, and cut. Returns the plan and the parts as cut. A recording within
    ``limit`` is one part, not cut (no file is written: the caller uses ``wav_path`` itself)."""
    if duration <= limit:
        return plan_parts(duration, [], target=target, limit=limit, window=window), []
    silences = await detect_silences(wav_path, duration)
    plan = plan_parts(duration, silences, target=target, limit=limit, window=window)
    cut = await cut_segments(wav_path, [p.start for p in plan[1:]], out_dir, total_seconds=duration, limit=limit)
    return plan, cut


__all__ = [
    "CUT_FORCED",
    "CUT_SILENCE",
    "CUT_START",
    "CutPart",
    "PlannedPart",
    "Silence",
    "SplitError",
    "check_plan",
    "cut_segments",
    "detect_silences",
    "parse_silencedetect",
    "plan_parts",
    "split_wav",
]
