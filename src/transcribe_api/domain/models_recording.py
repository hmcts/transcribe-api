from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import Column, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class JobStatus(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class BatchJobStatus(StrEnum):
    NOT_STARTED = "NotStarted"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"


class BaseTable(SQLModel):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    created_datetime: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_datetime: datetime | None = Field(default=None)


class WordInfo(SQLModel):
    text: str
    start_time: float
    end_time: float
    confidence: float


class NBestCandidate(SQLModel):
    """One Azure nBest candidate reading for a phrase.

    `text` is the candidate's display form (punctuated/capitalised);
    `lexical` is its raw recognised form. See DIAAT-232 spike
    (docs/spikes/DIAAT-232-nbest-alternatives.md) — Azure Speech Batch
    v3.2 never returns per-word alternatives, only whole alternate
    phrasings like this one.
    """

    text: str
    confidence: float | None = None
    lexical: str | None = None


class PhraseAlternatives(SQLModel):
    """The full nBest array Azure returned for one original recognised phrase.

    `candidates[0]` is Azure's own top choice — the same reading already
    surfaced as DialogueEntry.text/confidence/words — kept here too so the
    whole array round-trips untouched rather than being reconstructed from
    two different fields.

    start_word_index/end_word_index (inclusive, into the *merged* entry's
    `words` list) locate which words this phrase's alternatives apply to,
    the same convention WordCorrection uses. Both are None when no
    per-word alignment is available for this phrase (e.g. Azure returned
    no word-level detail, or a later speaker-grouping merge broke
    alignment) — the candidates still describe a whole alternative for
    *some* part of the segment even without a precise word range.
    """

    start_word_index: int | None = None
    end_word_index: int | None = None
    candidates: list[NBestCandidate]


class WordCorrection(SQLModel):
    """An active replacement for a contiguous run of the *original* words.

    Indices always refer to positions in DialogueEntry.words (never
    renumbered), so multiple non-overlapping corrections can coexist and
    still be rendered against the original per-word confidence/timing for
    everything outside the corrected ranges.
    """

    start_word_index: int
    end_word_index: int  # inclusive
    text: str


class CorrectionEntry(SQLModel):
    """One logged change to a segment's text — append-only audit trail.

    A "rollback" is just another entry (kind="rollback") rather than a
    destructive edit, so the full history always stays intact and visible.
    A clerk confirming a segment is correct as-is (without editing it) also
    logs an entry here (kind="accept_all") rather than inventing a separate
    mechanism — previous_text/new_text are identical for that kind, since no
    text actually changed; only DialogueEntry.accepted flips to True.
    """

    timestamp: str  # ISO 8601, set by the backend
    kind: str  # "segment" | "word_range" | "rollback" | "accept_all"
    previous_text: str  # effective *segment* text immediately before this change
    new_text: str  # effective *segment* text immediately after this change
    start_word_index: int | None = None  # set only for kind="word_range"
    end_word_index: int | None = None  # inclusive; set only for kind="word_range"
    # Just the phrase that actually changed — set only for kind="word_range",
    # so a UI can show "quick" -> "slow" instead of the whole (possibly very
    # long) segment text. previous_text/new_text stay whole-segment since
    # rollback_to_history_entry restores from them.
    previous_phrase: str | None = None
    new_phrase: str | None = None


class DialogueEntry(SQLModel):
    speaker: str
    text: str
    start_time: float
    end_time: float
    # Azure's own per-phrase confidence (0-1), not a verified accuracy
    # measurement — there's no human reference transcript to compare
    # against until a clerk corrects a segment (see corrected_text).
    confidence: float | None = None
    # Whole-segment freeform override — set by editing the segment's text
    # directly rather than a specific low-confidence phrase. Takes full
    # precedence over word_corrections when set. `text` is never mutated,
    # so the original auto-generated wording stays available to compute a
    # real word error rate against it.
    corrected_text: str | None = None
    # Active, non-overlapping replacements for specific runs of the
    # original words — lets the frontend keep showing per-word confidence
    # and playback-sync highlighting for everything the clerk hasn't
    # touched, instead of falling back to plain text for the whole segment.
    word_corrections: list[WordCorrection] | None = None
    correction_history: list[CorrectionEntry] | None = None
    # Per-word timing/confidence for the original (never corrected) text —
    # lets the frontend highlight individual low-confidence words and sync
    # highlighting to live playback position. None if Azure didn't return
    # word-level detail for this phrase.
    words: list[WordInfo] | None = None
    # Azure's other nBest readings for the phrase(s) making up this entry —
    # e.g. for a hover/click UI to offer as alternatives to a low-confidence
    # word or segment (see DIAAT-233/DIAAT-234). Grouped per original phrase
    # since alternatives are only ever whole alternate phrasings, never
    # per-word swaps (see docs/spikes/DIAAT-232-nbest-alternatives.md).
    # None if Azure returned no nBest data at all for this entry.
    alternatives: list[PhraseAlternatives] | None = None
    # Set by the "accept all" action — a clerk confirming a low-confidence
    # segment is correct as transcribed, without editing its text. Deliberately
    # independent of has_corrections()/corrected_text: accepting must not make
    # this segment count towards the word-error-rate calculation in
    # audio/accuracy.py (nothing was actually corrected), but it must still
    # remove the segment from "needs review" (see compute_accuracy()).
    accepted: bool = False

    def has_corrections(self) -> bool:
        return self.corrected_text is not None or bool(self.word_corrections)

    def effective_text(self) -> str:
        """Current text after any corrections — what a clerk would read today."""
        if self.corrected_text is not None:
            return self.corrected_text
        if self.word_corrections and self.words:
            parts: list[str] = []
            cursor = 0
            for wc in sorted(self.word_corrections, key=lambda w: w.start_word_index):
                parts.append(" ".join(w.text for w in self.words[cursor : wc.start_word_index]))
                parts.append(wc.text)
                cursor = wc.end_word_index + 1
            parts.append(" ".join(w.text for w in self.words[cursor:]))
            return " ".join(p for p in parts if p)
        return self.text


class CorrectionDatasetEntry(BaseTable, table=True):
    """A durable, queryable record of one clerk correction for future model training/eval.

    Distinct from DialogueEntry.correction_history (the per-job, in-place
    audit trail embedded in transcription_job.dialogue_entries): this table
    exists specifically so the accumulated corpus of (original ASR text,
    corrected text) pairs can later be exported wholesale — independent of
    any single job's lifecycle — to fine-tune or evaluate transcription
    models. One row per clerk correction (whole-segment or word-range);
    rollbacks are not recorded here since they don't produce a new training
    example.

    *** COMPLIANCE NOTE — READ BEFORE ENABLING IN ANY REAL ENVIRONMENT ***
    This table can capture real court-hearing transcript content (original
    and corrected wording from live hearings). Retention period and any
    anonymisation/redaction requirements for that content have NOT yet been
    defined, and legal/compliance sign-off has NOT happened (see DIAAT-231
    acceptance criteria #3). Writes to this table are gated behind
    `Settings.CORRECTIONS_DATASET_EXPORT_ENABLED`, which defaults to False
    for exactly this reason — do not set it to True in any environment that
    handles real hearing content until:
      1. a retention policy (how long rows are kept, and how/when they're
         purged) is agreed, and
      2. an anonymisation/redaction approach (if any is required) for real
         hearing content is agreed,
    both signed off by legal/compliance.
    """

    __tablename__ = "correction_dataset_entry"

    job_id: UUID = Field(foreign_key="transcription_job.id", index=True)
    # NULL for jobs submitted via JWT auth (no Caller row).
    caller_id: UUID | None = Field(default=None, foreign_key="caller.id", index=True)
    segment_index: int
    # "segment" | "word_range" — mirrors CorrectionEntry.kind, restricted to
    # the two actions that produce a genuine (original, corrected) pair.
    correction_kind: str
    start_word_index: int | None = Field(default=None)
    end_word_index: int | None = Field(default=None)
    speaker: str
    locale: str
    # The lexical (Speech Batch / ASR-generated) text this correction
    # replaced — always the original wording, never a previously-corrected
    # value — paired with corrected_text to form a (source, target) example.
    original_text: str
    corrected_text: str
    # Azure's phrase-level confidence (kind="segment") or the average
    # per-word confidence across the corrected range (kind="word_range");
    # None if Azure didn't return confidence for this phrase.
    confidence: float | None = Field(default=None)


# MERGE NOTE (architecture 3.3): both upstream services defined a `user` table,
# and it was the ONLY genuine table-name collision between them — the two
# `SpeechBatchJob` classes do not collide at the database level (recording used
# `transcription_job`, dictation `transcriptionjob`).
#
# The dictation model is a strict superset of the recording one: identical
# `email`, `azure_user_id` and `role`, plus `has_completed_onboarding` and the
# `transcriptions` relationship. So there is one `User`, defined in
# models_dictation, and re-exported here so recording-side imports keep working.
#
# Behaviour change to note: recording had `role: str | None = None`; the merged
# model uses dictation's `role: str = "Normal"` (indexed).
from transcribe_api.domain.models_dictation import User  # noqa: E402,F401  (re-export)


class Caller(BaseTable, table=True):
    __tablename__ = "caller"

    name: str = Field(index=True)
    hashed_key: str
    # SHA-256 of the raw API key, used as a fast indexed lookup before bcrypt verify.
    # Populated on key creation; NULL for legacy rows (fallback to linear scan).
    key_lookup_hash: str | None = Field(default=None, index=True)
    webhook_secret: str
    is_active: bool = Field(default=True)
    azure_app_id: str | None = Field(default=None)


class SpeechBatchJob(BaseTable, table=True):
    __tablename__ = "transcription_job"
    __table_args__ = (
        UniqueConstraint(
            "caller_id", "idempotency_key", name="uq_transcription_job_caller_idempotency"
        ),
    )

    # NULL for JWT-authenticated submissions (no Caller row exists).
    # Populated for API-key callers and pre-migration jobs.
    caller_id: UUID | None = Field(default=None, foreign_key="caller.id", index=True)
    # Nullable FK to the User table — populated for JWT-authenticated submissions.
    # NULL on rows created before auth migration (API-key-only callers).
    user_id: UUID | None = Field(default=None, foreign_key="user.id", index=True)
    status: JobStatus = Field(default=JobStatus.PENDING)

    # Submission fields
    audio_url: str
    locale: str = Field(default="en-GB")
    enable_diarization: bool = Field(default=True)
    callback_url: str | None = Field(default=None)
    idempotency_key: str | None = Field(default=None)
    metadata_: dict = Field(default_factory=dict, sa_column=Column("metadata", JSONB))

    # Results
    dialogue_entries: list = Field(default_factory=list, sa_column=Column(JSONB))
    error_message: str | None = Field(default=None)

    # A clerk-supplied reference transcript (e.g. a court reporter's
    # transcript), uploaded independently of the auto-generated dialogue
    # entries — lets a real word error rate be computed against the whole
    # transcription, not just the segments a clerk has corrected in this
    # app (see audio/accuracy.py). NULL until one is uploaded.
    # Explicit TEXT (not the default inferred VARCHAR) since a full hearing
    # transcript can be very large, and to match the baseline_transcript
    # migration exactly so Alembic autogenerate doesn't report a spurious
    # type diff.
    baseline_transcript: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    # Azure batch tracking
    batch_job_id: str | None = Field(default=None)
    batch_job_status: BatchJobStatus | None = Field(default=None)
    batch_job_url: str | None = Field(default=None)
    audio_duration_seconds: float | None = Field(default=None)
    audio_blob_path: str | None = Field(default=None)

    # Run metadata (DIAAT-227) — populated once the job reaches a terminal
    # state so the dashboard can surface "how long did this take and which
    # model produced it" without opening the transcript.
    transcription_duration_seconds: float | None = Field(default=None)
    model_identifier: str | None = Field(default=None)
    # Human-readable model name (DIAAT-243) resolved server-side by
    # dereferencing the Azure `model.self` URL held in model_identifier via
    # the Speech API (using the backend's existing Speech credentials). NULL
    # for historical jobs, jobs whose model_identifier is a non-URL fallback,
    # or when resolution failed — the dashboard falls back to
    # model_identifier in those cases. Never contains the subscription key.
    model_display_name: str | None = Field(default=None)

    # Cleanup
    needs_cleanup: bool = Field(default=False)
    cleanup_failure_reason: str | None = Field(default=None)

    # Webhook delivery guard — set atomically by the replica that wins the dispatch race.
    # NULL means no webhook has been dispatched yet for this job.
    webhook_dispatched_at: datetime | None = Field(default=None)
