"""Shared helpers of the DIAAT-408 tests of asynchronous upload preparation: real small audio files
made with ffmpeg, a SQLite job table, and a local-directory audio storage. No Azure, no large fixtures."""

from __future__ import annotations

import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlmodel import Session, SQLModel, create_engine

from transcribe_api.domain.models_recording import JobStatus, SpeechBatchJob, User

USER_ID = UUID("00000000-0000-0000-0000-000000000002")


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(_type, _compiler, **_kw):  # the unit suite has no Postgres
    return "JSON"


def new_engine(path: Path, *, foreign_keys: bool = False):
    """A SQLite job table in a file: each connection (each worker thread's session) is its own, as
    on Postgres, so claims and renewals really are separate transactions. ``foreign_keys`` makes SQLite
    enforce them as Postgres does (a part needs its job and goes with it; the job's owner must exist,
    so USER_ID is created): off by default, as most tests add jobs without their owners."""
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30})
    if foreign_keys:

        @event.listens_for(engine, "connect")
        def _foreign_keys_on(dbapi_connection, _record):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine)
    if foreign_keys:
        with Session(engine) as session:
            session.add(User(id=USER_ID, azure_user_id="oid", email="judge@example.test", role="Judge"))
            session.commit()
    return engine


_CODECS = {
    ".wav": ["-c:a", "pcm_s16le"],
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "32k"],
    ".m4a": ["-c:a", "aac", "-b:a", "32k"],
    ".flac": ["-c:a", "flac"],
    ".ogg": ["-c:a", "libvorbis", "-b:a", "32k"],
}


@cache
def tone(extension: str, seconds: float, *, extra: tuple[str, ...] = ()) -> bytes:
    """A real ``seconds``-long 440 Hz tone in the format ``extension`` names (small: 8 kHz mono)."""
    with tempfile.TemporaryDirectory(prefix="tone-") as tmp:
        out = Path(tmp) / f"tone{extension}"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={seconds}:sample_rate=8000",
                "-ac",
                "1",
                *_CODECS[extension],
                *extra,
                "-y",
                str(out),
            ],
            check=True,
        )
        return out.read_bytes()


def add_job(engine, **fields) -> SpeechBatchJob:
    """A PENDING job row; ``fields`` override anything."""
    upload_id = fields.pop("upload_id", uuid4())
    defaults = {
        "audio_url": "https://client.example/ignored.wav",
        "status": JobStatus.PENDING,
        "user_id": USER_ID,
        "audio_blob_path": f"uploads/{USER_ID}/{upload_id}-hearing.wav",
        "source_blob_path": f"incoming/{USER_ID}/{upload_id}.mp3",
        "created_datetime": datetime.now(UTC) - timedelta(minutes=1),
    }
    job = SpeechBatchJob(**{**defaults, **fields})
    with Session(engine) as session:
        session.add(job)
        session.commit()
        session.refresh(job)
    return job


def reload_job(engine, job_id) -> SpeechBatchJob | None:
    with Session(engine) as session:
        return session.get(SpeechBatchJob, job_id)


@cache
def tone_and_silence(pattern: tuple[tuple[str, float], ...], *, sample_rate: int = 16000) -> bytes:
    """A real 16 kHz mono 16-bit WAV of tones ("t") and silences ("s"), each the given seconds long,
    e.g. (("t", 20), ("s", 2), ("t", 20)). Small: 32 kB per second."""
    sources = []
    for kind, seconds in pattern:
        if kind == "t":
            sources.append(f"sine=frequency=440:duration={seconds}:sample_rate={sample_rate}")
        else:
            sources.append(f"anullsrc=d={seconds}:r={sample_rate}:cl=mono")
    command = ["ffmpeg", "-nostdin", "-v", "error"]
    for source in sources:
        command += ["-f", "lavfi", "-i", source]
    inputs = "".join(f"[{i}]" for i in range(len(sources)))
    command += ["-filter_complex", f"{inputs}concat=n={len(sources)}:v=0:a=1", "-ac", "1", "-c:a", "pcm_s16le"]
    with tempfile.TemporaryDirectory(prefix="mix-") as tmp:
        out = Path(tmp) / "mix.wav"
        subprocess.run([*command, "-y", str(out)], check=True)
        return out.read_bytes()
