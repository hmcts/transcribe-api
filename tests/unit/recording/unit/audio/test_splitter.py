"""DIAAT-408: splitting a recording into parts Azure Speech can take (stt/splitter.py).

The planner is pure, so it is checked over thousands of random durations and silence sets, for the
real limit (14,400 s) and for scaled-down ones. The cutting is checked with real ffmpeg on small
generated tone-and-silence audio and a small part target: never hours, never large fixtures.
"""

from __future__ import annotations

import asyncio
import itertools
import random
import wave

import pytest

from tests.unit.recording.unit.async_upload_helpers import tone_and_silence
from transcribe_api.stt import splitter
from transcribe_api.stt.splitter import (
    CUT_FORCED,
    CUT_SILENCE,
    CUT_START,
    SEGMENT_SLACK_SECONDS,
    PlannedPart,
    Silence,
    SplitError,
    check_plan,
    parse_silencedetect,
    plan_parts,
)

LIMIT = 14_400.0

# (The cut is now made in Python, exact to the sample; these bounds are the margin the planner was sized for, and
# the tolerance the tests keep, from when ffmpeg's segment muxer made it.)
# ffmpeg's segment muxer cut the 16 kHz mono 16-bit PCM WAV at packet boundaries, at the first packet at or
# after the planned time. With ffmpeg 8.0.1 a packet was about 2 KiB (0.06 s); the production image's 9.0.2 cuts
# 4096-byte packets: 4096 / (16,000 samples x 2 bytes) = 0.128 s, which is what moved a cut planned at 21.0 s
# to 21.12 s. A cut may therefore land up to PACKET_SECONDS late; the tolerance allows two packets (the second
# for the rounding of the plan and of the silence times), and the planner's slack must cover one.
PACKET_BYTES = 4096
BYTES_PER_SECOND = 16_000 * 2
PACKET_SECONDS = PACKET_BYTES / BYTES_PER_SECOND  # 0.128
CUT_TOLERANCE_SECONDS = 2 * PACKET_SECONDS  # 0.256


def _random_silences(rng: random.Random, duration: float) -> list[Silence]:
    """Sorted, non-overlapping silences of 0.5 s to 15 minutes, some touching the ends, some none at all."""
    silences, at = [], rng.choice([0.0, rng.uniform(0, 600)])
    while at < duration and rng.random() < 0.97:
        start = at + rng.expovariate(1 / max(duration / 40, 1.0))
        length = rng.choice([rng.uniform(0.5, 3), rng.uniform(3, 60), rng.uniform(60, 900)])
        if start >= duration:
            break
        silences.append(Silence(start, min(duration, start + length)))
        at = start + length + 0.001
    return silences


def _assert_plan(parts: list[PlannedPart], duration: float, limit: float) -> None:
    assert parts[0].start == 0.0 and parts[-1].end == duration  # covers [0, duration]
    assert [p.index for p in parts] == list(range(1, len(parts) + 1))
    for before, after in itertools.pairwise(parts):
        assert before.end == after.start  # contiguous
    for part in parts:
        assert 0 < part.duration <= limit  # a cut never leaves [0, duration]; no part over the limit
    assert parts[0].cut_kind == CUT_START and all(p.cut_kind in (CUT_SILENCE, CUT_FORCED) for p in parts[1:])


# The planner: invariants over many inputs ---------------------------------------------------


@pytest.mark.parametrize("seed", range(8))
def test_the_plan_satisfies_its_invariants_for_random_durations_and_silences_at_the_real_limit(seed):
    rng = random.Random(seed)
    for _ in range(400):
        duration = rng.choice([rng.uniform(1, 14_400), rng.uniform(14_400, 30_000), rng.uniform(30_000, 100_000)])
        target = rng.choice([14_400.0, 12_600.0, rng.uniform(600, 14_400)])
        window = rng.choice([600.0, rng.uniform(1, 3_000)])
        parts = plan_parts(duration, _random_silences(rng, duration), target=target, limit=LIMIT, window=window)
        _assert_plan(parts, duration, LIMIT)
        if duration <= LIMIT:
            assert len(parts) == 1 and parts[0].cut_kind == CUT_START
        else:
            assert len(parts) >= 2


@pytest.mark.parametrize("seed", range(6))
def test_the_plan_satisfies_its_invariants_with_scaled_down_limits(seed):
    rng = random.Random(1000 + seed)
    planned = 0
    for _ in range(500):
        limit = rng.uniform(5, 200)
        duration = rng.uniform(0.5, limit * 12)
        target = rng.uniform(1, limit)
        try:
            parts = plan_parts(
                duration, _random_silences(rng, duration), target=target, limit=limit, window=rng.uniform(0.1, 80)
            )
        except SplitError:
            # Only a limit too small to cut inside (the slack and a minimum part) may be refused.
            assert limit <= 2 * SEGMENT_SLACK_SECONDS + 1 or duration > limit
            continue
        _assert_plan(parts, duration, limit)
        planned += 1
    assert planned > 400  # nearly every input was planned, not refused


def test_a_recording_at_or_under_the_limit_is_one_part_whatever_the_target():
    assert plan_parts(14_400.0, [Silence(7_000, 7_100)], target=100.0) == [PlannedPart(1, 0.0, 14_400.0, CUT_START)]
    assert len(plan_parts(0.5, [], target=12_600.0)) == 1


def test_eight_hours_is_three_parts_of_about_two_hours_forty_with_the_default_target():
    parts = plan_parts(28_800.0, [], target=12_600.0)
    assert [round(p.duration) for p in parts] == [9_600, 9_600, 9_600]
    assert all(p.cut_kind in (CUT_FORCED, CUT_START) for p in parts)


def test_exactly_twice_the_limit_is_three_parts_because_a_cut_can_land_late():
    parts = plan_parts(28_800.0, [], target=14_400.0)
    assert len(parts) == 3 and max(p.duration for p in parts) <= LIMIT - SEGMENT_SLACK_SECONDS


def test_a_cut_goes_to_the_longest_silence_in_its_window():
    # 8 h in 3 parts: ideal cuts at 9,600 and 19,200; window 600.
    silences = [Silence(9_100, 9_102), Silence(9_900, 9_960), Silence(9_300, 9_310), Silence(19_190, 19_250)]
    parts = plan_parts(28_800.0, silences, target=12_600.0)
    assert (
        parts[1].start == pytest.approx(9_930.0) and parts[1].cut_kind == CUT_SILENCE
    )  # the 60 s one, cut in its middle
    assert parts[2].start == pytest.approx(19_220.0) and parts[2].cut_kind == CUT_SILENCE


def test_with_no_silence_in_the_window_the_cut_is_made_at_the_ideal_point_and_says_so():
    silences = [Silence(8_000, 9_000), Silence(10_400, 10_900)]  # both outside 9,600 +- 600
    parts = plan_parts(28_800.0, silences, target=12_600.0)
    assert parts[1].start == pytest.approx(9_600.0) and parts[1].cut_kind == CUT_FORCED


def test_a_silence_straddling_the_window_is_cut_where_it_overlaps_the_window():
    parts = plan_parts(28_800.0, [Silence(9_900, 12_000)], target=12_600.0)  # window ends at 10,200
    assert parts[1].start == pytest.approx(10_050.0)  # the middle of 9,900-10,200
    assert parts[1].cut_kind == CUT_SILENCE


def test_ties_go_to_the_silence_nearest_the_ideal_point():
    parts = plan_parts(28_800.0, [Silence(9_100, 9_160), Silence(9_540, 9_600)], target=12_600.0)
    assert parts[1].start == pytest.approx(9_570.0)


def test_the_window_never_exceeds_a_quarter_of_the_spacing_so_cuts_stay_in_order():
    # 3 s of audio in 2 parts with a 600 s window requested: a silence far from the middle is not reached.
    parts = plan_parts(30.0, [Silence(1.0, 2.0)], target=16.0, limit=20.0, window=600.0)
    assert parts[1].start > 10.0 and parts[1].cut_kind == CUT_FORCED


def test_a_part_never_exceeds_the_limit_even_when_the_best_silence_would_make_it_so():
    # Duration 2 x limit - 1: a silence at the very start of the window would put 14,399+ s in part 1.
    limit = 14_400.0
    duration = 2 * limit - 100.0
    parts = plan_parts(duration, [Silence(duration / 2 + 500, duration / 2 + 560)], target=limit)
    _assert_plan(parts, duration, limit)


def test_the_planner_does_not_touch_its_inputs():
    silences = [Silence(9_900, 9_960)]
    plan_parts(28_800.0, silences, target=12_600.0)
    assert silences == [Silence(9_900, 9_960)]


@pytest.mark.parametrize("duration", [0, -5, float("nan"), float("inf")])
def test_nonsense_durations_are_refused(duration):
    with pytest.raises(SplitError):
        plan_parts(duration, [], target=12_600.0)


def test_check_plan_fails_loudly_for_each_broken_invariant():
    ok = [PlannedPart(1, 0.0, 10.0, CUT_START), PlannedPart(2, 10.0, 20.0, CUT_SILENCE)]
    assert check_plan(ok, 20.0, 15.0) == ok
    broken = {
        "gap": [ok[0], PlannedPart(2, 11.0, 20.0, CUT_SILENCE)],
        "overlap": [ok[0], PlannedPart(2, 9.0, 20.0, CUT_SILENCE)],
        "short": [ok[0], PlannedPart(2, 10.0, 19.0, CUT_SILENCE)],
        "over the limit": [PlannedPart(1, 0.0, 16.0, CUT_START), PlannedPart(2, 16.0, 20.0, CUT_SILENCE)],
        "empty part": [ok[0], PlannedPart(2, 10.0, 10.0, CUT_SILENCE), PlannedPart(3, 10.0, 20.0, CUT_SILENCE)],
        "wrong index": [ok[0], PlannedPart(3, 10.0, 20.0, CUT_SILENCE)],
        "first is a cut": [PlannedPart(1, 0.0, 10.0, CUT_SILENCE), ok[1]],
        "none": [],
    }
    for name, plan in broken.items():
        with pytest.raises(SplitError):
            check_plan(plan, 20.0, 15.0)
        assert name


# silencedetect parsing ------------------------------------------------------------------------

REAL_FFMPEG_OUTPUT = """\
Input #0, wav, from 'x.wav':
  Duration: 00:00:08.00, bitrate: 256 kb/s
[silencedetect @ 0x7e0fc4002e40] silence_start: 3
[silencedetect @ 0x7e0fc4002e40] silence_end: 5.000063 | silence_duration: 2.000062
"""


def test_silences_are_read_from_ffmpegs_log():
    assert parse_silencedetect(REAL_FFMPEG_OUTPUT, 8.0) == [Silence(3.0, pytest.approx(5.000063))]


def test_a_silence_that_runs_to_the_end_of_the_file_has_no_end_line():
    assert parse_silencedetect("[silencedetect @ 0x1] silence_start: 7.25\n", 8.0) == [Silence(7.25, 8.0)]


def test_a_negative_start_is_clipped_to_zero_and_an_end_past_the_duration_to_the_duration():
    log = "silence_start: -0.021\nsilence_end: 1.5 | silence_duration: 1.521\nsilence_start: 9\nsilence_end: 12 | x\n"
    assert parse_silencedetect(log, 10.0) == [Silence(0.0, 1.5), Silence(9.0, 10.0)]


def test_log_lines_that_are_not_silences_are_ignored():
    assert parse_silencedetect("size=N/A time=00:00:08 bitrate=N/A speed=900x\n", 8.0) == []


async def test_silences_are_detected_in_real_audio(tmp_path):
    path = tmp_path / "in.wav"
    path.write_bytes(tone_and_silence((("t", 3.0), ("s", 2.0), ("t", 3.0))))
    (silence,) = await splitter.detect_silences(path, 8.0)
    assert silence.start == pytest.approx(3.0, abs=0.1) and silence.end == pytest.approx(5.0, abs=0.1)


# Cutting, with real ffmpeg --------------------------------------------------------------------


def _frames(path) -> int:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes()


async def test_a_recording_is_cut_at_its_silences_and_the_parts_add_up_to_the_whole(tmp_path):
    # 3 x 20 s of tone with 2 s silences between: 64 s. Limit 30 s, target 22 s: 3 parts, cuts in the silences.
    pattern = (("t", 20.0), ("s", 2.0), ("t", 20.0), ("s", 2.0), ("t", 20.0))
    wav = tmp_path / "full.wav"
    wav.write_bytes(tone_and_silence(pattern))
    duration = _frames(wav) / 16000
    out = tmp_path / "parts"
    out.mkdir()

    plan, cut = await splitter.split_wav(wav, duration, out, target=22.0, limit=30.0, window=6.0)

    assert len(plan) == len(cut) == 3
    assert [p.cut_kind for p in plan] == [CUT_START, CUT_SILENCE, CUT_SILENCE]
    assert plan[1].start == pytest.approx(21.0, abs=0.3) and plan[2].start == pytest.approx(43.0, abs=0.3)
    # Offsets and lengths are measured from the files; each part is within the limit; together they
    # are the whole recording to within a sample or two (the cuts snap to packets, the total does not move).
    assert cut[0].offset_seconds == 0.0
    for before, after in itertools.pairwise(cut):
        assert after.offset_seconds == pytest.approx(before.offset_seconds + before.duration_seconds, abs=1e-9)
    assert sum(_frames(p.path) for p in cut) == _frames(wav)
    assert max(p.duration_seconds for p in cut) <= 30.0
    # The planned and the actual cut differ by no more than a packet (0.06 s with ffmpeg 8.0.1, 0.128 s with 9.0.2).
    for planned, actual in zip(plan[1:], cut[1:], strict=False):
        assert actual.offset_seconds == pytest.approx(planned.start, abs=CUT_TOLERANCE_SECONDS)
        # ...and still inside the silence it was planned in (the planner cuts in the middle of a silence of
        # 2 s, so a packet late is a second from its end).
        assert (20.0 < actual.offset_seconds < 22.0) if planned.index == 2 else (42.0 < actual.offset_seconds < 44.0)


@pytest.mark.parametrize("seed", range(6))
def test_cuts_that_land_a_packet_late_still_leave_every_part_within_the_limit_and_inside_the_silence(seed):
    """The quantisation of ffmpeg 9.0.2 (0.128 s, here taken as 0.13) cannot be run locally (8.0.1 is
    installed), so it is checked by construction: shift every planned cut by up to a packet and the parts
    are still within the limit (the planner works to limit - SEGMENT_SLACK_SECONDS, one second, about
    eight packets). That a cut in a silence stays inside it follows from the next test."""
    from transcribe_api.stt.splitter import SILENCE_MIN_SECONDS

    quantum = 0.13
    assert quantum >= PACKET_SECONDS and SEGMENT_SLACK_SECONDS >= 7 * quantum
    assert SILENCE_MIN_SECONDS / 2 >= 1.9 * quantum  # half the shortest silence is two packets
    rng = random.Random(seed)
    for _ in range(300):
        limit = rng.uniform(20, 400)
        duration = rng.uniform(limit * 1.01, limit * 10)
        silences = _random_silences(rng, duration)
        parts = plan_parts(duration, silences, target=rng.uniform(5, limit), limit=limit, window=rng.uniform(1, 80))
        late = [0.0, *(rng.uniform(0, quantum) for _ in parts[1:]), 0.0]  # the first start and the last end stay
        actual = [p.start + late[i] for i, p in enumerate(parts)] + [duration]
        for before, after in itertools.pairwise(actual):
            assert after - before <= limit  # the part still fits Azure's limit


def test_a_cut_planned_in_the_shortest_silence_is_still_inside_it_a_packet_late():
    """The shortest silence silencedetect reports (SILENCE_MIN_SECONDS, 0.5 s), alone in the window: the plan cuts
    in its middle, a quarter of a second from either end, and a cut 0.13 s late is still inside."""
    from transcribe_api.stt.splitter import SILENCE_MIN_SECONDS

    silence = Silence(9_599.75, 9_599.75 + SILENCE_MIN_SECONDS)
    parts = plan_parts(28_800.0, [silence], target=12_600.0)
    cut = parts[1].start
    assert parts[1].cut_kind == CUT_SILENCE and cut == pytest.approx((silence.start + silence.end) / 2)
    assert silence.start < cut + 0.13 < silence.end


def _write_wav(path, seconds: float, rate: int = 8_000) -> None:
    """A mono 16-bit WAV whose samples count up (mod 2**15), so every part's content can be checked."""
    frames = round(seconds * rate)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        step = 100_000
        for start in range(0, frames, step):
            wav.writeframes(
                b"".join((n % 32768).to_bytes(2, "little") for n in range(start, min(frames, start + step)))
            )


async def test_more_than_a_thousand_real_parts_keep_their_order_and_offsets(tmp_path):
    """1,025 parts (a 205 s WAV cut every 0.2 s), with names past part-999: each follows the previous one and the
    offsets are the cut times, to the sample."""
    wav = tmp_path / "full.wav"
    wav.write_bytes(tone_and_silence((("t", 205.0),)))
    total = _frames(wav) / 16000
    cut_times = [round(0.2 * k, 3) for k in range(1, 1025)]
    out = tmp_path / "parts"
    out.mkdir()
    cut = await splitter.cut_segments(wav, cut_times, out, total_seconds=total, limit=60.0)
    assert len(cut) == 1025
    assert [p.path.name for p in cut] == [f"part-{n:03d}.wav" for n in range(1025)]
    assert [p.index for p in cut] == list(range(1, 1026))
    assert [p.offset_seconds for p in cut[:1025]] == pytest.approx([0.2 * n for n in range(1025)], abs=1 / 16000)
    assert sum(_frames(p.path) for p in cut) == _frames(wav)


async def test_cut_times_that_do_not_fit_on_one_command_line_are_cut_all_the_same(tmp_path):
    """ffmpeg's -segment_times took every cut time in ONE argument, and Linux allows 128 KiB in one: a recording
    of hours cut into parts of seconds (PART_TARGET_SECONDS is valid down to 1) could not even be started
    (E2BIG, and every retry the same). 15,499 cuts of 0.2 s written as the old code wrote them are 139 KB."""
    wav = tmp_path / "full.wav"
    _write_wav(wav, 3_100.0)
    cut_times = [round(0.2 * k, 3) for k in range(1, 15_500)]
    assert len(",".join(f"{t:.6f}" for t in cut_times)) > 128 * 1024  # the old argument
    out = tmp_path / "parts"
    out.mkdir()

    cut = await splitter.cut_segments(wav, cut_times, out, total_seconds=3_100.0, limit=60.0)

    assert len(cut) == 15_500
    assert [p.index for p in cut] == list(range(1, 15_501))
    assert [p.offset_seconds for p in cut] == pytest.approx([0.2 * n for n in range(15_500)], abs=1 / 8000)
    assert sum(_frames(p.path) for p in cut) == _frames(wav)


async def test_every_part_holds_exactly_the_samples_between_its_cuts(tmp_path):
    wav = tmp_path / "full.wav"
    _write_wav(wav, 3.0, rate=1000)  # 3,000 samples counting up
    out = tmp_path / "parts"
    out.mkdir()
    cut = await splitter.cut_segments(wav, [0.5, 1.2504], out, total_seconds=3.0, limit=10.0)
    assert [(p.offset_seconds, p.duration_seconds) for p in cut] == [
        (0.0, 0.5),
        (0.5, 0.75),
        (1.25, 1.75),
    ]  # 1.2504 -> 1250
    first_samples = []
    for part in cut:
        with wave.open(str(part.path), "rb") as w:
            first_samples.append(int.from_bytes(w.readframes(1), "little"))
    assert first_samples == [0, 500, 1250]


@pytest.mark.parametrize("cuts", [[2.0, 1.0], [1.0, 1.0], [0.0], [3.0], [4.0]])
async def test_cut_times_that_are_not_increasing_and_inside_the_recording_are_refused(tmp_path, cuts):
    wav = tmp_path / "full.wav"
    _write_wav(wav, 3.0, rate=1000)
    with pytest.raises(SplitError, match="increasing"):
        await splitter.cut_segments(wav, cuts, tmp_path, total_seconds=3.0)


async def test_cancelling_the_cut_stops_the_copy_before_the_caller_can_remove_the_directory(tmp_path, monkeypatch):
    """The copy runs in a thread. Cancelled, the task tells it to stop and waits for it: when the cancellation
    reaches the caller, nothing is writing any more. Deterministic: the copy is held until it has been told."""
    import threading

    wav = tmp_path / "full.wav"
    _write_wav(wav, 3.0, rate=1000)
    entered, told, ended = threading.Event(), [], threading.Event()
    real = splitter._copy_frames

    def held_copy(source, target, count, stop):
        entered.set()
        told.append(stop.wait(20))  # returns once cut_segments has asked it to stop
        ended.set()
        return real(source, target, count, stop)  # raises _Stopped: the stop is set

    monkeypatch.setattr(splitter, "_copy_frames", held_copy)
    task = asyncio.create_task(splitter.cut_segments(wav, [1.0], tmp_path, total_seconds=3.0))
    assert await asyncio.to_thread(entered.wait, 20)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert told == [True] and ended.is_set()  # told to stop, and finished before the cancellation was raised


async def test_audio_with_no_silence_is_cut_at_the_ideal_points(tmp_path):
    wav = tmp_path / "full.wav"
    wav.write_bytes(tone_and_silence((("t", 70.0),)))
    out = tmp_path / "parts"
    out.mkdir()
    plan, cut = await splitter.split_wav(wav, 70.0, out, target=25.0, limit=30.0, window=5.0)
    assert [p.cut_kind for p in plan] == [CUT_START, CUT_FORCED, CUT_FORCED]
    assert sum(_frames(p.path) for p in cut) == 70 * 16000


async def test_a_recording_within_the_limit_is_one_part_and_nothing_is_cut(tmp_path):
    wav = tmp_path / "full.wav"
    wav.write_bytes(tone_and_silence((("t", 10.0),)))
    plan, cut = await splitter.split_wav(wav, 10.0, tmp_path, target=25.0, limit=30.0)
    assert len(plan) == 1 and cut == []
    assert list(tmp_path.glob("part-*.wav")) == []


async def test_a_part_over_the_limit_after_cutting_fails_loudly(tmp_path):
    wav = tmp_path / "full.wav"
    wav.write_bytes(tone_and_silence((("t", 40.0),)))
    out = tmp_path / "parts"
    out.mkdir()
    with pytest.raises(SplitError, match="over the limit"):
        await splitter.cut_segments(wav, [10.0], out, total_seconds=40.0, limit=20.0)


async def test_parts_that_do_not_add_up_to_the_recording_fail_loudly(tmp_path):
    wav = tmp_path / "full.wav"
    wav.write_bytes(tone_and_silence((("t", 40.0),)))
    out = tmp_path / "parts"
    out.mkdir()
    with pytest.raises(SplitError, match="add up"):
        await splitter.cut_segments(wav, [10.0], out, total_seconds=41.0, limit=60.0)


async def test_ffmpeg_failing_is_a_split_error_not_a_crash(tmp_path):
    out = tmp_path / "parts"
    out.mkdir()
    (tmp_path / "bad.wav").write_bytes(b"not a wav")
    with pytest.raises(SplitError):
        await splitter.cut_segments(tmp_path / "bad.wav", [1.0], out, total_seconds=2.0)
    with pytest.raises(SplitError):
        await splitter.detect_silences(tmp_path / "bad.wav", 2.0)
