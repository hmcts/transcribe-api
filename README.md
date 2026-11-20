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

Rollback: turn the flag off (admin > Feature flags, no deploy). Jobs already deferred are still prepared by the worker. The
one case that fails is an upload whose `POST /jobs` call lands **after** the flag was turned off: `/jobs` decides by the flag, so
it ignores the waiting original and submits a WAV that was never made, and the job fails (the user uploads again). An upload takes
as long as the file takes to arrive, so it is minutes for a large file; turn the flag off at a quiet time. The `incoming/` prefix
holds originals no job claimed if a user uploads and never submits: a storage lifecycle rule on it, and an ephemeral-storage
request and limit for the worker pod, are infrastructure asks that are not in this repository (see the rollout notes).

### Recordings over 240 minutes (`long_recordings_enabled`, with `async_upload_preparation`)

Azure fails a file over 240 minutes, so with **both flags on** a recording longer than that (up to `MAX_AUDIO_DURATION_SECONDS`,
8 hours) is cut into parts by the worker, and each part is its own Azure batch job. A recording of 240 minutes or less is never cut:
it keeps the single-file path above, with no part rows. With either flag off the limit is still 240 minutes, and a longer one
fails exactly as before (the 4-hour sentence).

- **Cutting** (`stt/splitter.py`): `n = ceil(duration / PART_TARGET_SECONDS)` parts of equal length are aimed for; each boundary
  moves to the longest silence within about 10 minutes of its ideal point (ffmpeg `silencedetect`), or is cut at the ideal point if
  there is none (`cut_kind` on the part row says which). The plan is checked: contiguous, covering the whole recording, no part over
  14,400 s. The WAV is cut with `ffmpeg -c copy -f segment` (no re-encode); each part's offset and length are measured from the cut
  files, because the muxer cuts at packet boundaries (about 0.06 s).
- **Recording the parts**: one row per part in `transcription_job_part` (migration 0017): offset in the recording, length, cut kind,
  blob (`uploads/{user_id}/{upload_id}-{name}.partNN.wav`, derived from the job's own WAV name, never client-supplied), Azure job,
  status, attempts. The job stays one row; it keeps the idempotency key; it is `PENDING` while its parts are submitted and
  `SUBMITTED` once all are.
- **Submitting**: each part is stored and submitted as soon as its own blob is stored (part 1 does not wait for the others or for
  the full WAV, which is stored beside them for playback), at most `MAX_PARALLEL_PARTS` at a time. A failed part is retried alone,
  `PART_MAX_ATTEMPTS` times; then the job fails with "Part N of M of the recording could not be sent for transcription. Please
  upload the recording again.", the part blobs and the Azure jobs already made are deleted, and the part rows stay as the record.
- **Resuming**: the part rows are the record. A part whose Azure job is recorded is never stored or submitted again, so a crash or a
  restart mid-way (the job's lease runs out, or the worker hands it back on shutdown) continues with the parts not yet submitted, cutting
  the recording again at the recorded offsets and refusing a cut that no longer matches. The one window left is a crash after Azure
  accepted a part and before its row was updated: that part is submitted again and Azure keeps an Azure job nothing polls.
- **Deleting a job** while its parts are in flight (`DELETE /jobs/{id}`) deletes the part blobs and Azure jobs and the rows; the worker
  finds the rows gone at its next write, deletes the blob and Azure job it had just made, and stops.
- **Polling and assembling** (`BatchPollingService`, beside its single-file polling): a job in parts has no Azure job of its own
  (`batch_job_*` stay empty until it finishes), so it is polled through its part rows: the submitted or running parts of jobs
  that are still in flight, the one polled longest ago first (`FOR UPDATE SKIP LOCKED`, so two pollers take different parts, and
  a separate query from the single-file one, so neither starves the other). The job becomes `RUNNING` when its first part runs
  (`speech.job_running` once, for the job). When every part has succeeded the job is assembled: each part's results are parsed as
  for a single file, its speakers are grouped and numbered **within the part** (diarization is per file), every entry and word time
  is moved by the part's `offset_seconds` (the offsets measured when the recording was cut), and the parts are concatenated in order.
  The transcript and the SUCCEEDED state are written in **one conditional UPDATE** (`complete_multipart_job`): the transcript
  appears all at once or not at all; when two pollers assemble the same job, or a worker restarts halfway, exactly one UPDATE
  changes the row and only that call records usage, finishes the job in telemetry, sends the webhook and cleans up. A crash after
  that UPDATE leaves the part blobs and Azure jobs recorded on the part rows; a sweep in every poll deletes them.
- **A failed part fails the job at once** (a part Azure reports Failed, or whose results cannot be retrieved): "Part N of M of the
  recording could not be transcribed. Please upload the recording again." Every write that moves a job on is conditional on the job
  still being SUBMITTED or RUNNING, so a late result of another part cannot bring a failed job back. The Azure jobs and blobs of all
  the parts are deleted. There is no user-facing retry or resubmit route for a job (none existed for single-file jobs either):
  the clerk uploads the recording again. Resubmitting a part happens only while the job is being prepared (above).
- **Billing and duration**: the Speech usage recorded for the job is the sum of the parts' durations as Azure reported them (a part
  Azure gave no duration for counts at the length it was cut to, and the source is then `estimate`); the job's
  `audio_duration_seconds` stays the recording's length (it equals the sum of the parts by construction; if they ever differ by more
  than a second a warning is logged and the recording's length is kept). `transcription_duration_seconds` is the longest part's, as
  the parts run at the same time; the provenance names the first part's Azure job and model.
- **Cleanup**: on completion and on failure every part blob and Azure job is deleted with the same code (`cleanup_job_parts`); what
  cannot be deleted is recorded on the job (`needs_cleanup`, `cleanup_failure_reason`), never fails a finished job, and is tried
  again by a later poll (the flag is cleared when it succeeds). The part rows stay as the record, with the blob and Azure URLs cleared.
- **Stuck jobs**: a job in parts is reported as stuck (and counted in `transcription.jobs.stuck`) once it has run longer than
  `JOB_STUCK_PART_FACTOR` (1.5) times its **longest part**, not after the fixed `JOB_STUCK_RUNNING_SECONDS` (6 hours, which is for a
  single file). Single-file jobs are judged as before. The gauges count a job in parts once, whatever its parts.
- **Mock Speech** (`SPEECH_BACKEND=mock`) gives each part its own transcript ("Part 2 opens with speaker one." ...), so the merge and
  its offsets can be seen end to end.
- **Speakers are labelled by part**: diarization is per file, so "Speaker 0" of one part is not known to be "Speaker 0" of another,
  and the service never joins speakers across parts (it also never merges the last section of one part into the first of the
  next). Every speaker of a recording in parts carries its part: `Part 1 · Speaker 0`, `Part 1 · Speaker 1`, `Part 2 · Speaker 0`
  ... (the part number is zero-padded to the width of the part count, `Part 02 · ` of 12, so the labels sort in part order; the first
  part is labelled too, so that no label reads as spanning the recording). A recording in one piece is labelled exactly as before
  (`Speaker 0`). The clerk maps the speakers onto roles with the existing speaker editor (rename one section, or every section by
  that speaker); giving the labels of several parts the same name merges them into one speaker.
  **Limitation**: the same person is a separate speaker in each part until the clerk renames them, and the service does not try to
  match speakers between parts (an overlap-and-match heuristic was considered and left out: it needs a measured accuracy before it
  can be trusted). Cutting at silences such as adjournments helps, because roles often change there anyway.
  There is no Word or other export of a recording's transcript in this service or in the web app (the only export is the audio
  download, `audio.export`), so the labels reach no template.

| Setting | Default | Meaning |
|---|---|---|
| `PART_TARGET_SECONDS` | 12600 | Longest part aimed for (above). |
| `MAX_PARALLEL_PARTS` | 4 | Parts stored and submitted at once. Each in-flight part holds `BLOB_UPLOAD_CONCURRENCY` x 4 MiB of upload buffers. |
| `PART_MAX_ATTEMPTS` / `PART_RETRY_BACKOFF_SECONDS` | 3 / 5 | Attempts at one part before the job fails, and the wait (times the attempt number) between them. |
| `JOB_STUCK_PART_FACTOR` | 1.5 | A job in parts is stuck after this many times its longest part has run (above). |

Temporary disk for a recording in parts: the converted WAV and its parts (about 115 MB per hour each, so about 1.8 GB for 8 hours)
per job being prepared, beside the original while it is downloaded.

### Long recordings: rollout, rollback and operation (DIAAT-408)

**Why the background preparation exists.** The CFT frontend Application Gateway gives a backend 30 seconds to answer (setting
`benchnotes`, read on AAT and demo and not on prod, where the same module's default of 30 s is expected: confirm with the platform
team). Converting a recording inside `POST /uploads` takes about 13-14 s per hour of audio (measured from AAT telemetry), so a
recording of about two hours or more got a 504 although the server finished. With `async_upload_preparation` on, every request the
browser makes answers in seconds whatever the recording's length, up to the size backstop.

**The two flags and every setting.** Both flags are system feature flags (admin > Feature flags, no deploy), default off
(`ASYNC_UPLOAD_PREPARATION`, `LONG_RECORDINGS_ENABLED` set the defaults). Long recordings take effect only with both on.

| Setting | Default | What it costs, or what to know |
|---|---|---|
| `MAX_UPLOAD_BYTES` | 1073741824 (1 GiB) | The backstop on one upload. **The web pod's `MAX_UPLOAD_BYTES` environment variable must be the same number** (the web refuses a declared size over its own before any of the body is read). A larger figure than 1 GiB also needs the resumable block upload (below): one request through the gateway chain is untested above it. |
| `MAX_AUDIO_DURATION_SECONDS` | 28800 (8 h) | Applies in full only with both flags on; otherwise the limit is `min(this, 14400)`. |
| `PART_TARGET_SECONDS` | 12600 (3 h 30 m) | Longest part aimed for; 1 to 14400. |
| `MAX_PARALLEL_PARTS` | 4 | Parts stored and submitted at once: that many Azure batch jobs at once for one recording. Azure's concurrency quota for the Speech resource was not looked up (`SPEECH_CONCURRENT_JOBS_LIMIT` only drives a utilisation gauge and throttles nothing). |
| `BLOB_UPLOAD_CONCURRENCY` | 8 | Blocks sent in parallel per blob: 8 x 4 MiB of memory per upload in flight (API pod: the original, 512 Mi limit; worker: per part and for the full WAV). Only the asynchronous path uses it. |
| `PREPARE_POLL_INTERVAL_SECONDS` | 5 | How often the worker looks for jobs to prepare. |
| `PREPARE_MAX_CONCURRENT` | 2 | Jobs prepared at once by one worker: each needs temporary disk for its WAV and parts (about 115 MB per hour of audio each, so about 2 x 0.9 GB for 8 h, plus the original while it is downloaded and converted, up to `MAX_UPLOAD_BYTES`). The worker pod has no ephemeral-storage limit set. |
| `PREPARE_MAX_ATTEMPTS` | 3 | Attempts at a job before it fails (a crashed attempt counts). |
| `PREPARE_LEASE_SECONDS` / `PREPARE_HEARTBEAT_SECONDS` | 300 / 30 | A job being prepared is renewed every heartbeat; another worker may take it over once the lease has run out. A crash is therefore retried after up to 5 minutes. The lease must be at least twice the heartbeat. |
| `PREPARE_RETRY_BACKOFF_SECONDS` | 30 | Wait before a failed attempt is repeated (times the attempt number). |
| `PART_MAX_ATTEMPTS` / `PART_RETRY_BACKOFF_SECONDS` | 3 / 5 | Attempts at one part before the job fails, and the wait between them. |
| `JOB_STUCK_PART_FACTOR` | 1.5 | A job in parts is reported stuck after this many times its longest part has run (single-file jobs keep `JOB_STUCK_RUNNING_SECONDS`, 6 h). |

**Rollout order.** (1) Deploy the API, the worker and the web with both flags off: migrations 0016 and 0017 are additive and run at
startup, the previous release's pods keep working while it rolls out, and behaviour is exactly as before. (2) Ask the platform team
for the two infrastructure items below. (3) On AAT turn on `async_upload_preparation`, run part A of the checklist below. (4) On AAT
turn on `long_recordings_enabled`, run part B. (5) Repeat on prod once AAT has passed, with a few users first.

**Rollback.** Turn the flags off (no deploy). Long recordings go first: with `long_recordings_enabled` off a job already in parts
carries on (its worker and poller do not look at the flag), but a recording over 240 minutes uploaded later is refused. With
`async_upload_preparation` off, jobs already waiting for preparation are still prepared by the worker. The one case that fails is an
upload whose `POST /jobs` lands after the flag was turned off (see "Asynchronous upload preparation"). A job being prepared when the
flag is turned off is not affected; a job whose preparation needs the effective limit is checked against it when the worker
converts it: a recording over 240 minutes still waiting when `long_recordings_enabled` is turned off fails with the 4-hour sentence.

**Infrastructure asks (not in this repository).** (a) An ephemeral-storage request and limit for the worker pod (its disk use is
described above; none is set today). (b) A lifecycle rule on the `incoming/` prefix of the recordings storage account that deletes
blobs after a day (originals of uploads that never became a job are otherwise kept: the shared-infrastructure repository). (c) A
check of what disk the AKS nodes give the worker.

**Follow-up not built: resumable block upload and 4 GB.** `MAX_UPLOAD_BYTES` is one setting read by the API and the web, so the
planned move to 4 GB is that configuration plus an upload in blocks (start, block and complete calls, resumable after a drop). One
request through Front Door, Traefik and Caddy has not been tried above 1 GiB.

**Pilot-tuning constants.** Not measured on real hearings, so treat as first guesses: silence detection -35 dB for 0.5 s and a
cut window of 600 s (`stt/splitter.py`; a recording with constant background noise may find no silence and be cut at the ideal
point, which `cut_kind` records), the 1 s cutting slack, `PART_TARGET_SECONDS`, `PART_MAX_ATTEMPTS`, `PART_RETRY_BACKOFF_SECONDS`,
`JOB_STUCK_PART_FACTOR`, the `PREPARE_*` values and `MAX_PARALLEL_PARTS`.

**Known limitations.**
- The same person is a separate speaker in each part until the clerk renames them (no automatic matching).
- A crash after Azure accepted a part and before its row was updated submits that part again on resume and leaves an Azure job
  nothing polls (the single-file path has the same window).
- There is no retry or resubmit route for a failed job (single-file jobs never had one): the clerk uploads again.
- A recording's transcript cannot be exported (there is no export of one; only the audio can be downloaded).
- With the asynchronous path on, a file that `ffprobe` cannot judge is accepted by the upload and fails as a job. Observed with
  ffmpeg 8.0.1: text named `.mp3` makes `ffprobe` exit 0 and report no duration (its MP3 detection is lenient), so the upload
  cannot refuse it and the worker does: the job fails with "The audio file could not be decoded...". A file `ffprobe` refuses is
  a 422 in the request. The length of a VBR MP3 with no Xing header is an estimate in the header; the exact check on the converted
  WAV is the backstop.
- Nothing here has been run against real Azure Speech or Blob Storage.

**Measured locally (not on a pod, not with real hearings).** On a development machine (8 cores, ffmpeg 8.0.1), with synthetic
8 kHz mono 8 kbps MP3 tone files (3.6 MB per hour; real recordings decode more slowly): an 8 h file (28.8 MB) converted to the 16 kHz
mono WAV (922 MB) in about 10-16 s, silence detection over that WAV took about 11 s and cutting it into three parts about 4-5 s;
ffmpeg's own peak memory was about 55 MB in each step. The browser-facing requests, with the real routes over local storage,
answered in 0.35 s (`/uploads`, 3.6 MB) to 0.9 s (28.8 MB) and 0.03-0.13 s (`/jobs`). The heavy end-to-end test (opt-in, below) ran
in 4 min 28 s with a peak of 507 MB resident for the whole test process (the API, the services and the test; ffmpeg is a separate
process). AAT measured 13-14 s per hour of conversion on a pod for real uploads: expect the preparation of an 8 h recording to take
minutes there, and size the worker's disk from the table above, not from these figures.

**The end-to-end test.** `tests/unit/recording/long_e2e/` runs the real routes, the preparation service, the poller and real
ffmpeg with the mock Speech backend over a 1 h, a 4 h (the limit), a 5 h and an 8 h recording, the three refusals (4 h 01 m with both
flags off, with background preparation on and long recordings off, and 8 h 02 m), a corrupt file and a job deleted while its parts
are in flight. Every scenario runs in the normal suite at a scaled-down size (30 s stands for 240 minutes); the real sizes are
deselected unless `RUN_LONG_E2E=1` (`pytest tests/unit/recording/long_e2e -m long_e2e`, optionally `LONG_E2E_DIR` for the generated
audio and `LONG_E2E_REPORT` for a JSON of the timings; about 4 GB of temporary disk and 5 minutes).

### Operator checklist for the AAT validation (NOT YET EXECUTED)

Nothing below has been run: it is what to do after the first AAT deploy. Record the browser's time for each request (developer
tools), the job's status over time, and the App Insights events (`customEvents`, filtered on `hmcts.journey.id` = the job id).

**Before:** both flags off; the web pod's `MAX_UPLOAD_BYTES` unset or equal to the API's; the infrastructure asks above in place or
the risk accepted; `GET /api/flags` shows `max_recording_seconds: 14400`.

**Part A, `async_upload_preparation` on** (long off): `max_recording_seconds` is still 14400.

| File | Expected |
|---|---|
| `bad.mp3` (not audio) | A 422 "could not be decoded" in under 2 s, or (if `ffprobe` is lenient with it, as it was locally) a 201 and then a failed job with the same sentence. |
| `over.mp3` (4 h 01 m), `just-over.mp3` | A 422 with the 4-hour sentence in under 10 s, nothing stored. (A VBR MP3 without a Xing header may be judged by an estimate: check each way.) |
| `timing-60min.mp3`, `timing-75min`, `timing-120min.mp3` | 201 from `/uploads` and `/jobs` each in under 10 s, job `pending` ("being prepared"), then `processing`, then `completed`; no part rows. |
| `timing-180min.mp3`, `timing-B-180min-32kbps.mp3`, `under-3h59m.mp3` | The same; previously a 504 and a duplicate job. |
| `exact-240.mp3` (14,400 s) | Accepted, one Azure job, no part rows. What Azure does at exactly 240 minutes is unknown: record it. |
| `wav-near-cap.wav` (988 MB) | `/uploads` answers inside the gateway limit, or `MAX_UPLOAD_BYTES` (web and API) must come down. Not measured anywhere yet. |
| A job deleted while `pending` | No original left in `incoming/`; no WAV. |
| Flip the flag off with an upload in flight | Jobs already deferred still complete; an upload whose `/jobs` lands after fails and is re-uploaded (documented). |
| Telemetry | `preparation.started/completed` per job, no 504s, server time per request under 10 s; the stuck gauge does not trip for preparing jobs. |

**Part B, `long_recordings_enabled` on as well** (`max_recording_seconds` 28800):

| File | Expected |
|---|---|
| `over.mp3` (4 h 01 m) | Accepted; two parts; completes. |
| A 5 h recording | Two parts; the transcript's second half starts at the second part's offset (about 2.5 h), times monotonic; speakers labelled `Part 1 · Speaker N`, `Part 2 · Speaker N`; one `job.finished` with `job.part_count` 2. |
| An 8 h recording | Three parts, each at most 4 h (about 2 h 40 m with the defaults); record the wall clock of conversion, `splitting.completed`, each part's `job.part_finished` and the whole job; the worker pod's memory and disk while it prepares (`kubectl top` and ephemeral storage). |
| An 8 h 02 m recording | A 422 with the 8-hour sentence in under 10 s. |
| Speaker rename across parts | In the job page, rename `Part 1 · Speaker 0` and `Part 2 · Speaker 0` to the same name with "Replace all": one speaker. |
| A job deleted mid-parts | No part blob, Azure job or row left. |
| A part that fails (if one can be provoked) | The job fails at once naming the part; the others' Azure jobs and blobs are deleted. |
| Rollback | Turn `long_recordings_enabled` off: a job in parts finishes; a new 5 h upload is refused with the 4-hour sentence. |
| Both flags off | `over.mp3` is refused after the request converted it (slow: seconds per hour of audio), as before. |

Also record: Azure's behaviour for parts of about 2 h 40 m and 3 h 30 m (time and cost), whether `silencedetect` found a silence
in each cut window on real hearings (`split.forced_cut_count`), and whether `MAX_PARALLEL_PARTS` 4 hits an Azure concurrency limit.

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
