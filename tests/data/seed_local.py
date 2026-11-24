"""Seed your own local database with the dev personas and the demo recording jobs.

Run it once the migrations are applied, from the repository root:

    .venv/bin/python tests/data/seed_local.py

Safe to run again: missing jurisdictions, users and assignments are added, nothing of theirs is deleted, and
the demo jobs are replaced. It refuses to run unless ENVIRONMENT is local and the database is the developer's
own (runtime/local_dev_database.py).
"""

from __future__ import annotations

import io
import os
import sys
import wave
from functools import cache
from uuid import UUID, uuid4

os.environ.setdefault("ENVIRONMENT", "local")

from sqlalchemy.engine import Engine
from sqlmodel import Session, col, create_engine, select

from transcribe_api.domain.assignments import ensure_assignment
from transcribe_api.domain.auth.dev_personas import PERSONA_JURISDICTIONS, PERSONAS
from transcribe_api.domain.jurisdictions import IAC_JURISDICTION_ID, JurisdictionSettings
from transcribe_api.domain.models_dictation import Jurisdiction, User
from transcribe_api.domain.models_recording import (
    BatchJobStatus,
    CorrectionDatasetEntry,
    CorrectionEntry,
    DialogueEntry,
    JobStatus,
    SpeechBatchJob,
    WordCorrection,
    WordInfo,
)
from transcribe_api.runtime.local_dev_database import UnsafeDatabaseURLError, local_dev_database_url
from transcribe_api.runtime.settings_recording import get_settings
from transcribe_api.stt import local_storage

DEMO_JOB_OWNER = "judge-iac"
DEMO_JOB_JURISDICTION = "IAC"
LOCAL_API_URL = "http://127.0.0.1:8000"

# The ET has no template, and hearing recordings on, so its judge can try the recording pages.
_ET_SETTINGS = {"features": {"hearing_recordings": True}}
_MODEL_IDENTIFIER = "azure-speech-batch-transcription (en-GB)"
# Every demo job plays the same silent recording, which must outlast the latest timestamp in any of them.
_AUDIO_SECONDS = 10.0
_AUDIO_SAMPLE_RATE_HZ = 16_000
_CASE_REFERENCES = {
    "clean": "SEED - Clean transcript",
    "needs_review": "SEED - Needs review (2 flagged)",
    "corrected": "SEED - Corrections + WER",
    "baseline": "SEED - Baseline transcript WER",
    "failed": "SEED - Failed job",
    "running": "SEED - Stuck processing",
}


def _ensure_jurisdiction(session: Session, code: str, name: str) -> tuple[Jurisdiction, bool]:
    row = session.exec(select(Jurisdiction).where(Jurisdiction.code == code)).first()
    if row is not None:
        return row, False
    settings = JurisdictionSettings.model_validate(_ET_SETTINGS if code == "ET" else {}).model_dump(mode="json")
    row = Jurisdiction(code=code, name=name, enabled=True, settings=settings)
    if code == "IAC":
        row.id = IAC_JURISDICTION_ID
    session.add(row)
    session.flush()
    return row, True


def seed_personas(session: Session) -> dict[str, list[str]]:
    """Create the personas' missing jurisdictions, users and assignments; returns what was created. Flushed."""
    created: dict[str, list[str]] = {"jurisdictions": [], "users": [], "assignments": []}
    jurisdictions: dict[str, Jurisdiction] = {}
    for code, name in PERSONA_JURISDICTIONS.items():
        jurisdictions[code], made = _ensure_jurisdiction(session, code, name)
        if made:
            created["jurisdictions"].append(code)

    for persona in PERSONAS.values():
        user = session.exec(select(User).where(User.azure_user_id == persona.azure_user_id)).first()
        if user is None:
            user = User(email=persona.email, azure_user_id=persona.azure_user_id, name=persona.display_name)
            session.add(user)
            session.flush()
            created["users"].append(persona.name)
        for code, role in persona.assignments:
            _row, made = ensure_assignment(
                session,
                role=role,
                jurisdiction_id=jurisdictions[code].id if code else None,
                user_id=user.id,
            )
            if made:
                created["assignments"].append(f"{persona.name}: {role}@{code or 'global'}")
    return created


def demo_audio_unavailable() -> str | None:
    """Why the demo jobs' audio cannot be stored where the API reads it, or None if it can."""
    settings = get_settings()
    if settings.AUDIO_STORAGE_BACKEND != "local":
        return "AUDIO_STORAGE_BACKEND is not local"
    if not settings.LOCAL_AUDIO_BASE_URL:
        return "LOCAL_AUDIO_BASE_URL is not set"
    return None


@cache
def _silent_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(_AUDIO_SAMPLE_RATE_HZ)
        audio.writeframes(bytes(2 * int(_AUDIO_SECONDS * _AUDIO_SAMPLE_RATE_HZ)))
    return buffer.getvalue()


def _entry(speaker: str, text: str, start: float, end: float, confidence: float) -> DialogueEntry:
    words = text.split()
    span = (end - start) / len(words)
    return DialogueEntry(
        speaker=speaker,
        text=text,
        start_time=start,
        end_time=end,
        confidence=confidence,
        words=[
            WordInfo(
                text=word,
                start_time=round(start + i * span, 2),
                end_time=round(start + (i + 1) * span, 2),
                confidence=confidence,
            )
            for i, word in enumerate(words)
        ],
    )


def _base_job(scenario: str, owner_id: UUID) -> SpeechBatchJob:
    blob_name = f"uploads/seed/{scenario}-{uuid4()}.wav"
    local_storage.save(_silent_wav(), blob_name)
    return SpeechBatchJob(
        user_id=owner_id,
        audio_url=local_storage.build_url(blob_name),
        audio_blob_path=blob_name,
        audio_duration_seconds=_AUDIO_SECONDS,
        locale="en-GB",
        metadata_={
            "seed_scenario": scenario,
            "case_reference": _CASE_REFERENCES[scenario],
            "audio_file_name": f"{scenario}.wav",
        },
    )


def _succeeded(job: SpeechBatchJob, *entries: DialogueEntry) -> SpeechBatchJob:
    job.status = JobStatus.SUCCEEDED
    job.batch_job_status = BatchJobStatus.SUCCEEDED
    job.model_identifier = _MODEL_IDENTIFIER
    job.dialogue_entries = [entry.model_dump() for entry in entries]
    return job


def _clean(owner_id: UUID) -> SpeechBatchJob:
    return _succeeded(
        _base_job("clean", owner_id),
        _entry("Speaker 0", "All rise. This tribunal is now in session.", 0.0, 2.4, 0.97),
        _entry("Speaker 1", "Thank you. We are considering the appellant's case this morning.", 2.6, 6.1, 0.95),
        _entry("Speaker 0", "Counsel, please proceed with your submissions.", 6.3, 8.5, 0.96),
    )


def _needs_review(owner_id: UUID) -> SpeechBatchJob:
    # The two middle segments fall below the review threshold of 0.65.
    return _succeeded(
        _base_job("needs_review", owner_id),
        _entry("Speaker 0", "Can you confirm your full name for the record.", 0.0, 2.2, 0.94),
        _entry("Speaker 1", "Yes, it's — I believe the microphone cut out there.", 2.4, 5.0, 0.42),
        _entry("Speaker 0", "Please repeat that, the recording was unclear.", 5.2, 7.1, 0.58),
        _entry("Speaker 1", "Apologies, my name is on the sworn statement already filed.", 7.3, 9.9, 0.91),
    )


def _corrected(owner_id: UUID) -> SpeechBatchJob:
    segment_edit = _entry("Speaker 0", "The hering will reconvene at two o'clock.", 0.0, 2.3, 0.55)
    segment_edit.corrected_text = "The hearing will reconvene at two o'clock."
    segment_edit.correction_history = [
        CorrectionEntry(
            timestamp="2026-10-20T09:15:00+00:00",
            kind="segment",
            previous_text="The hering will reconvene at two o'clock.",
            new_text="The hearing will reconvene at two o'clock.",
        )
    ]

    word_edit = _entry("Speaker 1", "Counsel for the appellant may call there first witness.", 2.5, 5.8, 0.6)
    there = word_edit.text.split().index("there")
    word_edit.word_corrections = [WordCorrection(start_word_index=there, end_word_index=there, text="their")]
    word_edit.correction_history = [
        CorrectionEntry(
            timestamp="2026-10-20T09:16:30+00:00",
            kind="word_range",
            previous_text="Counsel for the appellant may call there first witness.",
            new_text="Counsel for the appellant may call their first witness.",
            start_word_index=there,
            end_word_index=there,
            previous_phrase="there",
            new_phrase="their",
        )
    ]

    accepted = _entry("Speaker 0", "Noted. Please proceed when ready.", 6.0, 7.8, 0.6)
    accepted.accepted = True
    accepted.correction_history = [
        CorrectionEntry(
            timestamp="2026-10-20T09:17:00+00:00",
            kind="accept_all",
            previous_text="Noted. Please proceed when ready.",
            new_text="Noted. Please proceed when ready.",
        )
    ]
    return _succeeded(_base_job("corrected", owner_id), segment_edit, word_edit, accepted)


def _baseline(owner_id: UUID) -> SpeechBatchJob:
    job = _succeeded(
        _base_job("baseline", owner_id),
        _entry("Speaker 0", "The witness will now take the oath.", 0.0, 2.1, 0.93),
        _entry("Speaker 1", "I swear to tell the truth, the whole truth.", 2.3, 4.6, 0.9),
    )
    # Close to the transcript but not identical, as a clerk's own typing would be: a small, non-zero WER.
    job.baseline_transcript = (
        "The witness will now take the oath.\nI swear to tell the truth, the whole truth, and nothing but the truth."
    )
    return job


def _failed(owner_id: UUID) -> SpeechBatchJob:
    job = _base_job("failed", owner_id)
    job.status = JobStatus.FAILED
    job.batch_job_status = BatchJobStatus.FAILED
    job.error_message = "Azure Batch Transcription failed: the audio could not be decoded."
    return job


def _running(owner_id: UUID) -> SpeechBatchJob:
    job = _base_job("running", owner_id)
    job.status = JobStatus.RUNNING
    job.batch_job_status = BatchJobStatus.RUNNING
    # Without a batch_job_url the poller never picks it up, so it stays running until it is deleted.
    job.batch_job_url = None
    return job


_SCENARIOS = (_clean, _needs_review, _corrected, _baseline, _failed, _running)


def seed_demo_jobs(session: Session) -> tuple[list[SpeechBatchJob], list[str]]:
    """Replace every seeded job with a fresh set; returns the new jobs and the audio of the replaced ones,
    which is only deleted once the replacement is committed. Flushed."""
    owner = session.exec(select(User).where(User.azure_user_id == PERSONAS[DEMO_JOB_OWNER].azure_user_id)).one()
    jurisdiction = session.exec(select(Jurisdiction).where(Jurisdiction.code == DEMO_JOB_JURISDICTION)).one()

    replaced = session.exec(select(SpeechBatchJob).where(col(SpeechBatchJob.metadata_).has_key("seed_scenario"))).all()
    if replaced:
        # Its job_id has no delete cascade.
        dataset_entries = select(CorrectionDatasetEntry).where(
            col(CorrectionDatasetEntry.job_id).in_([job.id for job in replaced])
        )
        for entry in session.exec(dataset_entries).all():
            session.delete(entry)
        session.flush()
        for job in replaced:
            session.delete(job)
        session.flush()

    jobs = [scenario(owner.id) for scenario in _SCENARIOS]
    for job in jobs:
        job.jurisdiction_id = jurisdiction.id
        session.add(job)
    session.flush()
    return jobs, [job.audio_blob_path for job in replaced if job.audio_blob_path]


def seed(engine: Engine, *, with_demo_jobs: bool) -> tuple[dict[str, list[str]], list[SpeechBatchJob]]:
    """Seed the personas, and the demo jobs if asked, in one transaction."""
    with Session(engine, expire_on_commit=False) as session:
        created = seed_personas(session)
        jobs, replaced_audio = seed_demo_jobs(session) if with_demo_jobs else ([], [])
        session.commit()
    for blob_name in replaced_audio:
        local_storage.delete(blob_name)
    return created, jobs


def main() -> int:
    if os.environ.get("ENVIRONMENT", "").lower() != "local":
        print("Refusing to run: ENVIRONMENT is not local.")
        return 1
    try:
        url = local_dev_database_url()
    except UnsafeDatabaseURLError as exc:
        print(f"Refusing to run against DATABASE_CONNECTION_STRING: {exc}. Check your .env.")
        return 1

    audio_unavailable = demo_audio_unavailable()
    engine = create_engine(url)
    try:
        created, jobs = seed(engine, with_demo_jobs=audio_unavailable is None)
    finally:
        engine.dispose()

    for kind, items in created.items():
        print(f"{kind}: {', '.join(items) if items else 'already there'}")
    if jobs:
        scenarios = ", ".join(job.metadata_["seed_scenario"] for job in jobs)
        print(f"demo jobs for {DEMO_JOB_OWNER} in {DEMO_JOB_JURISDICTION}: {scenarios}")
    else:
        print(
            f"demo jobs: not seeded, {audio_unavailable}. Their audio is kept on local disk: set "
            f"AUDIO_STORAGE_BACKEND=local and LOCAL_AUDIO_BASE_URL={LOCAL_API_URL} in .env, restart the API "
            "and run this again."
        )
    print()
    print(f"Sign in with: curl -X POST {LOCAL_API_URL}/api/auth/session -H 'X-Dev-Persona: judge-et'")
    print(f"Personas: {', '.join(PERSONAS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
