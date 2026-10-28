# transcribe-api

The single HMCTS Transcribe backend — one CNP component (`product: transcribe`,
`component: api`) merging the two upstream services.

Built per the target architecture: https://tools.hmcts.net/confluence/pages/viewpage.action?pageId=2004000371

## Provenance

Code is imported from the upstream `main` branches, never edited in place:

| Upstream | Branch / commit |
|---|---|
| `hmcts/batch-audio-transcription` | `main` @ 988d797 |
| `hmcts/courtstranscribe` | `main` @ 2d73b43 |

Re-run the import reproducibly:

```bash
./scripts-import.sh <dir-containing-both-clones>
.venv/bin/python scripts-rewrite-imports.py
.venv/bin/python scripts-rename-recording-job.py
```

## Module layout

`domain` may import capabilities; capabilities must not import `domain`. That
one-way rule is what keeps the transcription capability extractable later.

```
src/transcribe_api/
├── stt/         capability A + C — file transcription (batch + fast), quality
├── realtime/    capability B — Speech token brokering, live draft
├── documents/   capability D + E — LLM orchestration, docx generation
├── domain/      capability F — users, roles, tags, templates, audit
├── runtime/     cross-cutting leaves — settings, db, blob, logging, security
├── api/         HTTP surface and the merged app factory
└── worker.py    background pollers, run as a separate workload
```

## API surfaces

Both upstream surfaces are preserved unchanged, and they do not collide:

- `/api/v1/*` — recording (13 paths). The router carries its own prefix.
- `/api/*` — dictation (32 paths)
- `/api/admin/*` — dictation privileged (14 paths)

## Local development

```bash
docker-compose up -d          # postgres + azurite
cp .env.example .env
uv sync --extra dev
./run-local.sh                # http://127.0.0.1:8000/docs
```

`ENVIRONMENT=local` activates the shared auth library's local-dev bypass, which
injects a mock identity with **all** roles. Never deploy with it.

Background pollers do not run in the API process. Set `RUN_WORKERS=true`, or
run `python -m transcribe_api.worker` as a separate workload.

Azure Speech is called with the pod's workload identity (an Entra token for the
custom-subdomain `AZURE_SPEECH_ENDPOINT`, `stt/speech_auth.py`); set
`AZURE_SPEECH_KEY` only for local development against a key-enabled account.

Uploads are converted with `ffmpeg` and probed with `ffprobe`, which the image
provides (Dockerfile). Install both to exercise uploads locally; deployed pods
refuse to start without them.

## Testing the recording flow locally without Azure

The recording flow (`/api/v1/*`) can run end to end — upload, submit, poll,
review, correct, accuracy/WER — with no Azure Speech resource and no real
Azure Blob Storage at all. Add to `.env` (see `.env.example`):

```bash
AUDIO_STORAGE_BACKEND=local      # writes uploads to disk instead of Azure Blob
LOCAL_AUDIO_BASE_URL=http://127.0.0.1:8000
SPEECH_BACKEND=mock              # skips every Azure Speech Batch call
RUN_WORKERS=true                 # so this one process also runs the poller
```

Restart the API, then:

```bash
.venv/bin/python scripts-seed-local.py       # once: creates the Caller API key
                                              # transcribe-web's upload button needs
.venv/bin/python scripts-seed-demo-data.py   # seeds 7 jobs covering every review-UI state
```

`scripts-seed-demo-data.py` seeds: a clean transcript, one flagged for review,
one with corrections and history already applied, one with a baseline
transcript uploaded for WER, a failed job, a job stuck mid-processing, and one
attributed to the API-key Caller rather than a signed-in user. It's safe to
re-run — it replaces its own rows (tagged via `metadata.seed_scenario`) rather
than accumulating duplicates, and refuses to run unless
`DATABASE_CONNECTION_STRING` resolves to a loopback host, so a misconfigured
`.env` can't point it at a shared database.

`SPEECH_BACKEND=mock` always returns the same fixed two-speaker transcript —
it never inspects the actual audio content, only that the upload decodes as
valid audio. Submitting a *real* job while mock mode is active is safe: the
mock only completes job URLs it created itself (`mock://batch-jobs/...`); a
genuine Azure job falls through to the real Azure path regardless of the
flag.

Dictation (`/api/*`) has no local-only path — it's still hardcoded to real
Azure Blob Storage. Both `AUDIO_STORAGE_BACKEND` and `SPEECH_BACKEND` default
to `"azure"` and must never be set in a deployed environment.

## Database migrations

One Alembic history in `alembic/`. Its first revision, `0001_baseline`, replaced
the two histories inherited from the dictation and recording services. Deployed
environments set `RUN_MIGRATIONS=true`, and the API runs `upgrade head` at
startup under a Postgres advisory lock (`runtime/migrations.py`), so the pods
starting together migrate one at a time.

After changing a model, generate a revision against an up-to-date database:

```bash
DATABASE_CONNECTION_STRING=postgresql://... .venv/bin/alembic revision --autogenerate -m "describe the change"
```

Check that the history still matches the models against a throwaway Postgres:

```bash
TEST_POSTGRES_URL=postgresql://... .venv/bin/python -m pytest tests/unit/test_migrations.py
```

Existing courtstranscribe and batch-audio-transcription databases are not on
this history yet; moving them over needs a schema comparison and
`alembic stamp 0001`.

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

## Outstanding

- Settings are still two modules (`settings_recording`, `settings_dictation`).
- `passlib` pins Python `<3.13` (it imports the removed stdlib `crypt`).
  Replacing it with direct `bcrypt` calls unlocks 3.13+.
