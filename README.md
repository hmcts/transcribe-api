# transcribe-api

The single HMCTS Transcribe backend — one CNP component (`product: transcribe`,
`component: api`) merging the two upstream services.

Built per the target architecture: https://tools.hmcts.net/confluence/pages/viewpage.action?pageId=2004000371

Architecture and delivery diagrams: [docs/architecture](docs/architecture/README.md).

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

## Branches and environments

`master` is the only long-lived branch (trunk-based).

- Branch from `master` and open the PR into `master`. Each PR gets its own preview at `transcribe-api-pr-<n>.preview.platform.hmcts.net` (HMCTS VPN needed), backed by a throwaway in-cluster Postgres. Add the `enable_keep_helm` label to keep the preview alive after the build.
- Merging to `master` deploys to AAT, then prod.
- Demo runs the images promoted to prod, via cnp-flux-config image policies (hmcts/cnp-flux-config#48971). The `demo` branch is the CNP branch for the demo Terraform only, and every `master` build overwrites it. Demo-only settings go in cnp-flux-config `apps/transcribe/transcribe-api/demo.yaml`.

## Preview environments

Every PR build deploys the API to the CFT preview cluster at `https://transcribe-api-pr-<n>.preview.platform.hmcts.net` (HMCTS VPN needed).

- Keeping it: CNP uninstalls the release at the end of each build. Add the GitHub label `enable_keep_helm` to keep it running after the build. Labels are read when the build starts, so rebuild after adding one.
- Deleting it: hmcts/cnp-pr-cleanup-aks deletes the release when the PR is closed or merged. Removing the label uninstalls it at the end of the next build.
- Sign-in: there is none. Previews set `ENVIRONMENT=preview` and `AUTH_PREVIEW_USER_ENABLED=true`, so a request without an `Authorization` header is treated as a fixed preview user (`preview-user`) holding `AUTH_PREVIEW_USER_ROLES`, still checked against `AUTH_ACCESS_ROLES`. A request that sends a token is verified as usual. The API refuses to start with the flag on in any other environment. See `domain/auth/token_auth.py`.
- Data is throwaway: Postgres and Azurite run in the release, and Speech is mocked (`SPEECH_BACKEND=mock`). The preview cluster shuts down every night, which wipes both. Azurite is also recreated on every push, so blobs go while database rows stay.
- What works: hearing recordings, transcribed with the mock (a fixed transcript), and dictation uploads (`PUT /api/upload-audio`, picked up by the blob poller, also given a fixed transcript). Storage is Azurite through `AZURE_STORAGE_CONNECTION_STRING`; SAS URLs are signed with Azurite's account key and point at the in-cluster Azurite host, so only server-side flows use them (`DIRECT_SDK_ACCESS` stays off). Minutes and titles need Langfuse and Azure OpenAI, which previews do not have, so those steps fail and the job finishes degraded (`minutes.failed`); the transcript and its Word document are still saved. No email is sent. `POST /api/live-transcription/submission` also works. Live dictation (the browser Speech SDK) is not available: `GET /api/get-speech-token` returns 500 "Azure Speech Services not configured".
- Web previews will embed this API (to be added in transcribe-web), using this chart with the same preview settings.

## Local development

```bash
docker compose up -d          # postgres + azurite
cp .env.example .env
uv sync --extra dev
./run-local.sh                # http://127.0.0.1:8000/docs
```

`ENVIRONMENT=local` lets `POST /api/auth/session` sign in as a **dev persona**
(`X-Dev-Persona: judge-et`, `multi`, `many`, `sysadmin`, `no-role`, ...) instead of an Entra token. Create
them with `.venv/bin/python scripts-seed-personas.py`; the token the API returns is what every other
route then requires. See [`docs/authz-token-and-context.md`](docs/authz-token-and-context.md). Put an
`APP_JWT_SIGNING_KEY` in `.env` (`.env.example` says how to generate one); deployed
environments must set it (the API refuses to start without it). Never deploy with `ENVIRONMENT=local`.

Background pollers do not run in the API process. Set `RUN_WORKERS=true`, or
run `python -m transcribe_api.worker` as a separate workload.

Azure Speech is called with the pod's workload identity (an Entra token for the
custom-subdomain `AZURE_SPEECH_ENDPOINT`, `stt/speech_auth.py`); set
`AZURE_SPEECH_KEY` only for local development against a key-enabled account.

Uploads are converted with `ffmpeg` and probed with `ffprobe`, which the image
provides (Dockerfile). Install both to exercise uploads locally; deployed pods
refuse to start without them.

## Telemetry

Logging, tracing and metrics come from [hmcts-otel](https://github.com/hmcts/hmcts-otel), set up once per
process by `runtime/telemetry.py`: logs are JSON and redacted (emails, tokens, appeal references, recording upload
file names), every request gets an `X-Request-Id`, and health probes are not traced.

- `APPLICATIONINSIGHTS_CONNECTION_STRING` exports to Application Insights; unset, telemetry goes to stdout only.
  Deployed, it is mounted from Key Vault (`AppInsightsConnectionString`) for the API and the worker.
- `OTEL_SERVICE_NAME` is `transcribe-api` for the API and `transcribe-api-worker` for the worker.
- There is no Sentry: errors go to Application Insights. `SENTRY_DSN` is ignored.
- The base image's `/opt/otel` distro is kept off `PYTHONPATH` (Dockerfile), so Azure Monitor is configured once.

## Recording length and size limits (DIAAT-408)

How long a recording may be, and how large its file, are settings and two system feature flags, all read in one place
(`stt/recording_limits.py`, `runtime/settings_recording.py`). Azure batch transcription with diarization fails a file over
240 minutes, so a longer recording has to be split before it is submitted; until that is done by the service, only 240 minutes
are accepted.

| Setting | Default | Meaning |
|---|---|---|
| `MAX_AUDIO_DURATION_SECONDS` | 28800 (8 h) | The longest recording the service can accept, as decoded. It applies in full only while both flags below are on; otherwise the limit is `min(this, 14400)`. |
| `PART_TARGET_SECONDS` | 12600 (3 h 30 m) | The longest part a recording over 240 minutes is split into. 1 to 14400 (Azure's per-file limit). |
| `MAX_PARALLEL_PARTS` | 4 | How many parts of one recording are prepared and submitted at once. |
| `MAX_UPLOAD_BYTES` | 1 GiB | Backstop on the size of one upload (HTTP 413 above it). **transcribe-web reads the same figure from its own `MAX_UPLOAD_BYTES` environment variable: set both to the same value.** A move to 4 GB needs this and the resumable block upload (not built). |

| Flag (admin > Feature flags) | Default setting | Effect |
|---|---|---|
| `async_upload_preparation` | `ASYNC_UPLOAD_PREPARATION=false` | `POST /uploads` stores the original and answers at once; the worker converts it and submits the job. |
| `long_recordings_enabled` | `LONG_RECORDINGS_ENABLED=false` | Recordings over 240 minutes, up to `MAX_AUDIO_DURATION_SECONDS`. Takes effect only with `async_upload_preparation` on. |

Both off: behaviour is as before DIAAT-408 (240 minutes, everything done inside the request). The effective limit is
what `GET /api/flags` returns as `max_recording_seconds`, which the web app shows on the upload page.

### Asynchronous upload preparation (`async_upload_preparation`)

Converting a recording takes about 13-14 s per hour of audio, and the frontend Application Gateway gives a backend 30 s, so a
long recording used to end in a 504 although the server finished. With the flag on, no request converts anything:

1. `POST /api/v1/uploads` saves the body, reads the length from the file header with `ffprobe` (refusing, within seconds, a file
   that is not audio, or longer than the effective limit, with the usual sentences), stores the **original** as
   `incoming/{user_id}/{upload_id}{ext}` (blocks sent `BLOB_UPLOAD_CONCURRENCY` = 8 at a time) and answers 201 as before:
   `blob_name` is still the WAV's future name under `uploads/{user_id}/`, so the ownership checks are unchanged.
2. `POST /api/v1/jobs` finds that original by the user's and the upload's ids (never by anything the client sends) and creates the
   job `PENDING` with `source_blob_path` set and **no Azure job**: it does not call Azure Speech. An upload with no original
   (made before the flag was turned on) is submitted at once, as before.
3. The worker's third task, `PreparationService` (`stt/preparation_service.py`, beside the batch poller and the dictation poller),
   claims such jobs, converts the original (`normalize_to_wav`), checks the exact length of the WAV, stores it, submits the job
   through the same code as a request (`submit_existing_job`) and deletes the original. A job that cannot be converted or is too
   long by the exact check becomes `FAILED` with the sentence the upload gives for the same file (the web already shows
   `error_message`); a storage failure is retried `PREPARE_MAX_ATTEMPTS` times and then fails the job; a crash or a restart
   leaves the original and the job `PENDING`, and the job is claimed again once its lease (`PREPARE_LEASE_SECONDS`, renewed every
   `PREPARE_HEARTBEAT_SECONDS`) has run out. A job is `PENDING` throughout; no new status exists.

| Setting | Default | Meaning |
|---|---|---|
| `PREPARE_POLL_INTERVAL_SECONDS` | 5 | How often the worker looks for jobs to prepare. |
| `PREPARE_MAX_CONCURRENT` | 2 | Jobs prepared at once. Each needs temporary disk for the original and the WAV (up to about 1.5 GB for a 1 GiB upload); the worker pod has no ephemeral-storage limit set. |
| `PREPARE_MAX_ATTEMPTS` | 3 | Attempts before a job fails. A crashed attempt counts. |
| `PREPARE_LEASE_SECONDS` / `PREPARE_HEARTBEAT_SECONDS` | 300 / 30 | A job being prepared is renewed every heartbeat; another attempt may take it over once the lease has run out. The lease must be at least twice the heartbeat. |
| `PREPARE_RETRY_BACKOFF_SECONDS` | 30 | Wait before a failed attempt is repeated (times the attempt number). |
| `BLOB_UPLOAD_CONCURRENCY` | 8 | Parallel blocks when storing the original or the WAV (memory: that many 4 MiB blocks). |

Rollback: turn the flag off (admin > Feature flags, no deploy). Jobs already waiting are still prepared by the worker. The
`incoming/` prefix holds originals no job claimed if a user uploads and never submits: a storage lifecycle rule on it (not in
this repository) should delete them after a day. Recordings over 240 minutes are not split yet: until the part model is built,
a job over 240 minutes fails in the worker with the 4-hour sentence even with `long_recordings_enabled` on, so leave that flag off.

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
