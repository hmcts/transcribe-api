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
3. ``cut_segments``: the PCM frames of the WAV are copied into the parts in Python, at the frame
   nearest each planned cut: no re-encode, close to disk speed, exact to the sample, and nothing
   goes through a command line (ffmpeg's segment muxer took every cut time in ONE argument, and
   Linux limits an argument to 128 KiB: a few thousand cuts, as a small ``PART_TARGET_SECONDS`` gives
   for a long recording, could not even be started; the muxer also cut at packet boundaries, which
   differ between ffmpeg versions). The length and offset of each part are still measured from the
   files; the parts are the whole recording to the sample.

Why the planner works to ``limit - SEGMENT_SLACK_SECONDS``: a cut may land a little after the planned
point (it is a margin that was sized for packet boundaries, up to 0.128 s; now a cut is within half a
sample), and a part must still be within Azure's limit when it does.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import re
import threading
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
# A margin for a cut landing after the planned point: sized for the packet boundaries ffmpeg's segment
# muxer cut at (0.128 s with 9.0.2's 4096-byte packets), which is eight times that. Kept: it costs
# nothing against hours, and a plan that fits it fits an exact cut.
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


# The plan's own arithmetic (cuts are floats) may leave parts touching to within rounding, so "contiguous"
# and "covering" are checked to this tolerance, a microsecond; a real gap or overlap is far larger (cuts
# are at least a sample, 62 microseconds, apart, and the checks that follow measure the cut files).
PLAN_TOLERANCE_SECONDS = 1e-6


def check_plan(parts: list[PlannedPart], duration: float, limit: float) -> list[PlannedPart]:
    """Assert the plan's invariants, loudly: contiguous, covering [0, duration] (to ``PLAN_TOLERANCE_SECONDS``), every part
    positive and at most ``limit`` seconds, indices 1..n, the first part's start not a cut. Returns
    the plan."""
    if not parts:
        raise SplitError("empty plan")
    if abs(parts[0].start) > PLAN_TOLERANCE_SECONDS or abs(parts[-1].end - duration) > PLAN_TOLERANCE_SECONDS:
        raise SplitError("the plan does not cover the recording")
    for i, part in enumerate(parts):
        if part.index != i + 1:
            raise SplitError("part indices are not 1..n")
        if not part.end > part.start:
            raise SplitError(f"part {part.index} is empty or reversed")
        if part.duration > limit:
            raise SplitError(f"part {part.index} is {part.duration:.1f} s, over the limit of {limit:.0f} s")
        if i and abs(part.start - parts[i - 1].end) > PLAN_TOLERANCE_SECONDS:
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


# Frames copied at a time: 64 Ki frames of 16 kHz mono 16-bit audio is 128 KiB, so memory stays small however
# long the recording, and a stop request is noticed within a few milliseconds of disk time.
_COPY_CHUNK_FRAMES = 65_536


class _Stopped(Exception):
    """The cut was abandoned because the task awaiting it was cancelled."""


def _copy_frames(source: wave.Wave_read, target: wave.Wave_write, count: int, stop: threading.Event) -> None:
    """Copy ``count`` frames from ``source`` (at its current position) to ``target``."""
    while count > 0:
        if stop.is_set():
            raise _Stopped
        chunk = source.readframes(min(_COPY_CHUNK_FRAMES, count))
        if not chunk:
            raise SplitError("the recording ended before the last cut")
        target.writeframes(chunk)
        count -= len(chunk) // source.getsampwidth() // source.getnchannels()


def _cut_wav(wav_path: Path, cut_times: list[float], out_dir: Path, stop: threading.Event) -> list[Path]:
    """Write ``out_dir/part-000.wav`` ... from ``wav_path``, cut at the frame nearest each of ``cut_times``.
    Pure Python on a PCM WAV (the recording is the 16 kHz mono WAV ``normalize_to_wav`` made): the cuts are
    exact to the sample, and nothing depends on a command line, whose length is limited (Linux allows
    128 KiB in one argument, which a few thousand cut times in one ``-segment_times`` exceed)."""
    try:
        with wave.open(str(wav_path), "rb") as source:
            rate, total = source.getframerate(), source.getnframes()
            bounds = [0, *(round(t * rate) for t in cut_times), total]
            if any(not 0 < b < total for b in bounds[1:-1]) or any(b <= a for a, b in itertools.pairwise(bounds)):
                raise SplitError("the cut times are not increasing and inside the recording")
            paths: list[Path] = []
            for number, (start, end) in enumerate(itertools.pairwise(bounds)):
                path = out_dir / f"part-{number:03d}.wav"
                with wave.open(str(path), "wb") as target:
                    target.setparams((*source.getparams()[:3], 0, "NONE", "not compressed"))
                    _copy_frames(source, target, end - start, stop)
                paths.append(path)
            return paths
    except wave.Error as exc:
        raise SplitError(f"the recording could not be cut ({exc})") from exc
    except (EOFError, OSError) as exc:
        raise SplitError(f"the recording could not be cut ({type(exc).__name__})") from exc


async def cut_segments(
    wav_path: Path,
    cut_times: list[float],
    out_dir: Path,
    *,
    total_seconds: float,
    limit: float = AZURE_BATCH_FILE_LIMIT_SECONDS,
) -> list[CutPart]:
    """Cut ``wav_path`` at ``cut_times`` into ``out_dir/part-000.wav`` ... (no re-encode: the PCM frames
    are copied; see ``_cut_wav``). Returns the parts with their offsets and lengths, measured from the files,
    after checking that there are ``len(cut_times) + 1`` of them, that none is longer than ``limit``, and that
    together they are the whole recording (to within a hundredth of a second). Raises SplitError otherwise.

    The copy runs in a thread. If this task is cancelled the thread is told to stop and is waited for (it
    notices within one chunk), so nothing is still writing into ``out_dir`` when the caller removes it."""
    stop = threading.Event()
    work = asyncio.ensure_future(asyncio.to_thread(_cut_wav, wav_path, cut_times, out_dir, stop))
    try:
        files = await asyncio.shield(work)
    except BaseException:
        stop.set()
        # The thread stops at the next chunk (or has already ended). Wait until it HAS, however often this
        # task is cancelled meanwhile: the caller removes the directory (and lets go of the job) as soon as
        # this raises. A further cancellation is remembered and raised, after the wait, in place of the first.
        later: asyncio.CancelledError | None = None
        while not work.done():
            try:
                await asyncio.wait({work})
            except asyncio.CancelledError as again:
                later = again
        if not work.cancelled():
            work.exception()  # its own outcome (_Stopped, an error) is not wanted: mark it retrieved
        if later is not None:
            raise later
        raise
    if len(files) != len(cut_times) + 1:
        raise SplitError(f"expected {len(cut_times) + 1} parts, wrote {len(files)}")
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
