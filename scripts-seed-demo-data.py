#!/usr/bin/env python3
"""Seed the local database with a handful of realistic recording jobs.

Covers the states the review UI and accuracy/WER logic actually branch on,
so local testing has something to click through for each without waiting on
a real transcription or hand-crafting data via the UI:

  clean            succeeded, high confidence throughout, nothing to review
  needs_review     succeeded, two low-confidence segments flagged for review
  corrected        succeeded, a mix of corrected/accepted segments with history
  baseline         succeeded, with a reference transcript uploaded for WER
  failed           a job that failed during transcription
  running          stuck mid-processing (no batch_job_url, so the poller
                    never touches it — stays this way until deleted)
  caller_submitted succeeded, attributed to the seeded API-key Caller rather
                    than a JWT user (the machine/webhook ingress path)

Requires SPEECH_BACKEND=mock and AUDIO_STORAGE_BACKEND=local (see .env) —
this writes directly to the same Postgres + local-disk audio store the
running API uses, bypassing Azure entirely either way.

Re-running this script replaces its own rows (tagged via
metadata_.seed_scenario) rather than accumulating duplicates. Local
development only.
"""

from __future__ import annotations

import os
import subprocess
import sys
import wave
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("ENVIRONMENT", "local")

from sqlmodel import Session, col, create_engine, select  # noqa: E402
from sqlalchemy import cast, String  # noqa: E402

from transcribe_api.domain.models_recording import (  # noqa: E402
    BatchJobStatus,
    Caller,
    CorrectionDatasetEntry,
    CorrectionEntry,
    DialogueEntry,
    JobStatus,
    SpeechBatchJob,
    WordCorrection,
    WordInfo,
)
from transcribe_api.domain.models_dictation import User  # noqa: E402
from transcribe_api.runtime.local_dev_database import (  # noqa: E402
    UnsafeDatabaseURLError,
    local_dev_database_url,
)
from transcribe_api.stt import local_storage  # noqa: E402

MOCK_USER_AZURE_ID = "local-dev-user-123"
SEED_AUDIO_SOURCE = Path(__file__).parent / "scripts-seed-demo-audio.wav"
# Every seeded job shares this one file and reports this as its duration, so
# it must cover the latest timestamp used by ANY scenario below (currently
# needs_review's last segment, ending at 9.9s) -- otherwise seeking to a
# later segment goes past the end of the audio and playback/word-timing
# highlighting can't be exercised there.
_AUDIO_DURATION_SECONDS = 10.0


def _entry(speaker: str, text: str, start: float, end: float, confidence: float, **kw) -> DialogueEntry:
    words = kw.pop("words", None)
    if words is None:
        # Evenly split timing across words when no explicit word list is given.
        parts = text.split()
        span = (end - start) / max(len(parts), 1)
        words = [
            WordInfo(
                text=w,
                start_time=round(start + i * span, 2),
                end_time=round(start + (i + 1) * span, 2),
                confidence=confidence,
            )
            for i, w in enumerate(parts)
        ]
    return DialogueEntry(
        speaker=speaker, text=text, start_time=start, end_time=end, confidence=confidence, words=words, **kw
    )


def _existing_audio_covers_duration() -> bool:
    if not SEED_AUDIO_SOURCE.exists():
        return False
    with wave.open(str(SEED_AUDIO_SOURCE), "rb") as f:
        return f.getnframes() / f.getframerate() >= _AUDIO_DURATION_SECONDS


def _ensure_seed_audio() -> None:
    """Generate the shared seed source file if it's missing or too short.

    Gitignored rather than committed (scripts-seed-demo-audio.wav would be a
    generated binary blob in version control for no benefit — ffmpeg is
    already a required local-dev dependency per the README, so regenerating
    it here is simpler than asking a developer to run this by hand). Also
    regenerates an existing file left over from before
    _AUDIO_DURATION_SECONDS was increased, rather than silently reusing one
    too short to cover every scenario's timestamps.
    """
    if _existing_audio_covers_duration():
        return
    print(f"Generating {SEED_AUDIO_SOURCE.name} (silent, {_AUDIO_DURATION_SECONDS:g}s, 16kHz mono)...")
    subprocess.run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            str(_AUDIO_DURATION_SECONDS),
            "-y",
            str(SEED_AUDIO_SOURCE),
        ],
        check=True,
        capture_output=True,
    )


def _attach_audio(job_id, blob_tag: str) -> tuple[str, str]:
    blob_name = f"uploads/seed/{blob_tag}-{uuid4()}.wav"
    local_storage.save_file(SEED_AUDIO_SOURCE, blob_name)
    return blob_name, local_storage.build_url(blob_name)


# What each scenario demonstrates, shown as the job's case reference in the
# UI (lib/recording/api-client.ts reads metadata.case_reference) instead of
# a bare UUID — so the jobs list is self-explanatory at a glance.
_CASE_REFERENCES = {
    "clean": "SEED - Clean transcript",
    "needs_review": "SEED - Needs review (2 flagged)",
    "corrected": "SEED - Corrections + WER",
    "baseline": "SEED - Baseline transcript WER",
    "failed": "SEED - Failed job",
    "running": "SEED - Stuck processing",
    "caller_submitted": "SEED - Caller-submitted (no user)",
}


def _base_job(scenario: str, user_id=None, caller_id=None) -> SpeechBatchJob:
    blob_name, audio_url = _attach_audio(scenario, scenario)
    return SpeechBatchJob(
        user_id=user_id,
        caller_id=caller_id,
        audio_url=audio_url,
        audio_blob_path=blob_name,
        audio_duration_seconds=_AUDIO_DURATION_SECONDS,
        locale="en-GB",
        metadata_={
            "seed_scenario": scenario,
            "case_reference": _CASE_REFERENCES[scenario],
            "audio_file_name": f"{scenario}.wav",
        },
    )


def _scenario_clean(user_id) -> SpeechBatchJob:
    job = _base_job("clean", user_id=user_id)
    job.status = JobStatus.SUCCEEDED
    job.batch_job_status = BatchJobStatus.SUCCEEDED
    job.model_identifier = "azure-speech-batch-transcription (en-GB)"
    job.dialogue_entries = [
        e.model_dump()
        for e in [
            _entry("Speaker 0", "All rise. This tribunal is now in session.", 0.0, 2.4, 0.97),
            _entry(
                "Speaker 1",
                "Thank you. We are considering the appellant's case this morning.",
                2.6,
                6.1,
                0.95,
            ),
            _entry("Speaker 0", "Counsel, please proceed with your submissions.", 6.3, 8.5, 0.96),
        ]
    ]
    return job


def _scenario_needs_review(user_id) -> SpeechBatchJob:
    job = _base_job("needs_review", user_id=user_id)
    job.status = JobStatus.SUCCEEDED
    job.batch_job_status = BatchJobStatus.SUCCEEDED
    job.model_identifier = "azure-speech-batch-transcription (en-GB)"
    job.dialogue_entries = [
        e.model_dump()
        for e in [
            _entry("Speaker 0", "Can you confirm your full name for the record.", 0.0, 2.2, 0.94),
            # Low segment-level confidence: flagged for review (threshold is 0.65).
            _entry("Speaker 1", "Yes, it's — I believe the microphone cut out there.", 2.4, 5.0, 0.42),
            _entry("Speaker 0", "Please repeat that, the recording was unclear.", 5.2, 7.1, 0.58),
            _entry("Speaker 1", "Apologies, my name is on the sworn statement already filed.", 7.3, 9.9, 0.91),
        ]
    ]
    return job


def _scenario_corrected(user_id) -> SpeechBatchJob:
    job = _base_job("corrected", user_id=user_id)
    job.status = JobStatus.SUCCEEDED
    job.batch_job_status = BatchJobStatus.SUCCEEDED
    job.model_identifier = "azure-speech-batch-transcription (en-GB)"

    whole_segment_edit = _entry("Speaker 0", "The hering will reconvene at two o'clock.", 0.0, 2.3, 0.55)
    whole_segment_edit.corrected_text = "The hearing will reconvene at two o'clock."
    whole_segment_edit.correction_history = [
        CorrectionEntry(
            timestamp="2026-10-20T09:15:00+00:00",
            kind="segment",
            previous_text="The hering will reconvene at two o'clock.",
            new_text="The hearing will reconvene at two o'clock.",
        )
    ]

    word_range_edit = _entry("Speaker 1", "Counsel for the appellant may call there first witness.", 2.5, 5.8, 0.6)
    # Zero-indexed word list: Counsel(0) for(1) the(2) appellant(3) may(4)
    # call(5) there(6) first(7) witness.(8) -- "there" is index 6, not 7.
    word_range_edit.word_corrections = [WordCorrection(start_word_index=6, end_word_index=6, text="their")]
    word_range_edit.correction_history = [
        CorrectionEntry(
            timestamp="2026-10-20T09:16:30+00:00",
            kind="word_range",
            previous_text="Counsel for the appellant may call there first witness.",
            new_text="Counsel for the appellant may call their first witness.",
            start_word_index=6,
            end_word_index=6,
            previous_phrase="there",
            new_phrase="their",
        )
    ]

    accepted_as_is = _entry("Speaker 0", "Noted. Please proceed when ready.", 6.0, 7.8, 0.6)
    accepted_as_is.accepted = True
    accepted_as_is.correction_history = [
        CorrectionEntry(
            timestamp="2026-10-20T09:17:00+00:00",
            kind="accept_all",
            previous_text="Noted. Please proceed when ready.",
            new_text="Noted. Please proceed when ready.",
        )
    ]

    job.dialogue_entries = [e.model_dump() for e in [whole_segment_edit, word_range_edit, accepted_as_is]]
    return job


def _scenario_baseline(user_id) -> SpeechBatchJob:
    job = _base_job("baseline", user_id=user_id)
    job.status = JobStatus.SUCCEEDED
    job.batch_job_status = BatchJobStatus.SUCCEEDED
    job.model_identifier = "azure-speech-batch-transcription (en-GB)"
    job.dialogue_entries = [
        e.model_dump()
        for e in [
            _entry("Speaker 0", "The witness will now take the oath.", 0.0, 2.1, 0.93),
            _entry("Speaker 1", "I swear to tell the truth, the whole truth.", 2.3, 4.6, 0.9),
        ]
    ]
    # Deliberately close-but-not-identical to the auto-generated text above,
    # so baseline_word_error_rate comes out nonzero but small — a clerk's
    # independently-typed reference transcript, not a perfect copy.
    job.baseline_transcript = (
        "The witness will now take the oath.\nI swear to tell the truth, the whole truth, and nothing but the truth."
    )
    return job


def _scenario_failed(user_id) -> SpeechBatchJob:
    job = _base_job("failed", user_id=user_id)
    job.status = JobStatus.FAILED
    job.batch_job_status = BatchJobStatus.FAILED
    job.error_message = "Azure Batch Transcription failed: the audio could not be decoded."
    return job


def _scenario_running(user_id) -> SpeechBatchJob:
    job = _base_job("running", user_id=user_id)
    job.status = JobStatus.RUNNING
    job.batch_job_status = BatchJobStatus.RUNNING
    # No batch_job_url: the poller's _fetch_pending_jobs skips rows without
    # one, so this stays "processing" indefinitely instead of completing on
    # the next poll tick (which SPEECH_BACKEND=mock would otherwise do in
    # seconds). Delete and re-seed if you want a fresh one.
    job.batch_job_url = None
    return job


def _scenario_caller_submitted(caller_id) -> SpeechBatchJob:
    job = _base_job("caller_submitted", caller_id=caller_id)
    job.status = JobStatus.SUCCEEDED
    job.batch_job_status = BatchJobStatus.SUCCEEDED
    job.model_identifier = "azure-speech-batch-transcription (en-GB)"
    job.dialogue_entries = [
        e.model_dump()
        for e in [
            _entry("Speaker 0", "This hearing was submitted via the machine ingress.", 0.0, 2.6, 0.92),
            _entry("Speaker 1", "Confirmed — attributed to a Caller, not a signed-in user.", 2.8, 5.4, 0.9),
        ]
    ]
    return job


def main() -> int:
    try:
        _ensure_seed_audio()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"Could not generate seed audio with ffmpeg: {exc}")
        print("Install ffmpeg (required for local dev regardless, see README) and retry.")
        return 1

    try:
        database_url = local_dev_database_url()
    except UnsafeDatabaseURLError as exc:
        print(f"Refusing to run against DATABASE_CONNECTION_STRING: {exc}. Check your .env.")
        return 1
    engine = create_engine(database_url)

    with Session(engine) as session:
        user = session.exec(select(User).where(User.azure_user_id == MOCK_USER_AZURE_ID)).first()
        if not user:
            print(
                f"No User row for azure_user_id={MOCK_USER_AZURE_ID!r} yet — "
                "log in locally once (any page load) so the mock identity is "
                "upserted, then re-run this script."
            )
            return 1

        caller = session.exec(select(Caller).where(Caller.name == "local-dev-frontend")).first()
        if not caller:
            print("No 'local-dev-frontend' Caller yet — run scripts-seed-local.py first.")
            return 1

        scenarios = [
            _scenario_clean(user.id),
            _scenario_needs_review(user.id),
            _scenario_corrected(user.id),
            _scenario_baseline(user.id),
            _scenario_failed(user.id),
            _scenario_running(user.id),
            _scenario_caller_submitted(caller.id),
        ]

        tags = [s.metadata_["seed_scenario"] for s in scenarios]
        existing = session.exec(
            select(SpeechBatchJob).where(
                cast(SpeechBatchJob.metadata_["seed_scenario"], String).in_([f'"{t}"' for t in tags])
            )
        ).all()
        existing_ids = [row.id for row in existing]
        # CorrectionDatasetEntry.job_id has no delete cascade (only written
        # when CORRECTIONS_DATASET_EXPORT_ENABLED=true, off by default, but a
        # developer may have flipped it on while poking at a seeded job
        # through the real UI). Delete those first so removing the parent
        # job doesn't hit a foreign-key violation and fail the transaction.
        if existing_ids:
            for entry in session.exec(
                select(CorrectionDatasetEntry).where(col(CorrectionDatasetEntry.job_id).in_(existing_ids))
            ).all():
                session.delete(entry)
        for row in existing:
            session.delete(row)
        session.commit()
        # Audio files are deleted only after the database transaction above
        # has actually succeeded. local_storage.delete() is a plain
        # filesystem call with no rollback tied to the DB session, so doing
        # this before commit would leave a job's audio gone from disk while
        # its (rolled-back) row still existed, if the commit had failed.
        for row in existing:
            if row.audio_blob_path:
                local_storage.delete(row.audio_blob_path)
        if existing:
            print(f"Removed {len(existing)} previously-seeded row(s) and their audio files.")

        for job in scenarios:
            session.add(job)
        session.commit()

        for job in scenarios:
            session.refresh(job)
            print(f"  {job.metadata_['seed_scenario']:<20} {job.status:<10} id={job.id}")

    print()
    print(f"Seeded {len(scenarios)} jobs. View them at http://localhost:3000/recording")
    return 0


if __name__ == "__main__":
    sys.exit(main())
