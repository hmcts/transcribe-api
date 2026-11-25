# Long recordings (DIAAT-408)

Recordings of up to 20 hours, and uploads that never wait on the 30 second limit of the frontend Application Gateway: how the
service accepts, prepares, cuts, submits and assembles them, how to roll the flags out and back, and what to check on AAT. The
[README](../README.md) has the short version and the settings that matter day to day.

Nothing here has been run against real Azure Speech, Azure Blob Storage or a real Application Gateway; the operator checklist
at the end has not been executed.

## The limits and the two flags

How long a recording may be, and how large its file, are settings and two system feature flags, all read in one place
(`stt/recording_limits.py`, `runtime/settings_recording.py`). Azure batch transcription with diarization fails a file over
240 minutes, so a longer recording is split into parts before it is submitted, and only with both flags below on is more than 240 minutes
accepted.

| Setting | Default | Meaning |
|---|---|---|
| `MAX_AUDIO_DURATION_SECONDS` | 72000 (20 h) | The longest recording the service can accept, as decoded. It applies in full only while both flags below are on; otherwise the limit is `min(this, 14400)`. |
| `PART_TARGET_SECONDS` | 12600 (3 h 30 m) | The longest part a recording over 240 minutes is split into. 1 to 14400 (Azure's per-file limit). |
| `MAX_PARALLEL_PARTS` | 4 | How many parts of one recording are prepared and submitted at once. |
| `MAX_UPLOAD_BYTES` | 1 GiB | Backstop on the size of one upload (HTTP 413 above it). **transcribe-web reads the same figure from its own `MAX_UPLOAD_BYTES` environment variable: set both to the same value.** 1 GiB is the agreed limit for now; a larger one would also need an upload in blocks that can resume (not built, no follow-up planned). |

| Flag (admin > Feature flags) | Default setting | Effect |
|---|---|---|
| `async_upload_preparation` | `ASYNC_UPLOAD_PREPARATION=false` | `POST /uploads` stores the original and answers at once; the worker converts it and submits the job. |
| `long_recordings_enabled` | `LONG_RECORDINGS_ENABLED=false` | Recordings over 240 minutes, up to `MAX_AUDIO_DURATION_SECONDS`. Takes effect only with `async_upload_preparation` on. |

Both off: the limit is 240 minutes and the upload converts the file inside the request, as before DIAAT-408, except that the
length is first read from the file header, so an over-long or unreadable file is refused at once (see "How an upload is checked").
The effective limit is what `GET /api/flags` returns as `max_recording_seconds`, which the web app shows on the upload page.

## How an upload is checked, and asynchronous preparation (`async_upload_preparation`)

**The header check, whatever the flag.** Every upload is saved and its length is read from the file header with `ffprobe`
(`_refuse_from_header` in `api/routes_recording.py`) before anything is converted or stored: a file that is not audio is a 422
("could not be decoded"), a missing `ffprobe` is a 503, and a recording over the effective limit is a 422 with the usual
sentence, all within seconds. The header is not the length of the audio Azure will see, so only what is *certainly* over the
limit is refused here (`stt/recording_limits.py`, `stt/preprocessing.py`):

- The length is that of the **first audio stream**, the one the conversion maps (`ffmpeg -map 0:a:0`), not the container's: an MP4 or
  MKV whose video is longer than its audio is measured by its audio. If that stream states no duration (Matroska does not) and the
  file has any other stream (a video, or a later and possibly longer audio stream), the length is unknown, because the container's
  is that of the longest stream; the container's is used only when the audio stream is the file's only stream.
- A figure from a format that states the length (WAV, FLAC, Ogg, MP4/M4A, Matroska, or an MP3 with a Xing/LAME header) is exact to
  a fraction of a second, but encoder delay and padding differ between ffmpeg versions (8.0.1 here, 9.0.2 in the production image), so
  it is refused only if it is more than `HEADER_TOLERANCE_SECONDS` (2 s) over the limit.
- A figure that is an **estimate** (an MP3 with no Xing/LAME header, raw AAC, any format not listed: size over bitrate; measured
  locally at +0.8 % on a constant-bitrate file and -7 % on a variable-bitrate one) is refused only if it is over the limit by a
  quarter (`ESTIMATE_MARGIN` 1.25). An MP3 counts as exact only when `ffprobe` reports a non-zero start time, which is the encoder
  delay from the Xing/LAME header (verified on files made with `ffmpeg -write_xing 0` and without); one with a Xing header but no
  delay counts as an estimate, which only refuses less.
- A file `ffprobe` reads that has **no audio stream at all** (a video-only MP4) is a 422 "could not be decoded", with the flag on or
  off, before anything is stored; "no audio stream" is not "audio of unknown length".
- A file that states no length passes, and so does anything between the limit and these bounds: the exact check after the
  conversion (in the request, or in the worker) decides.

Converting a recording takes about 13-14 s per hour of audio, and the frontend Application Gateway gives a backend 30 s, so a
long recording used to end in a 504 although the server finished. What happens next depends on the flag.

**Flag off.** The request goes on as it did before DIAAT-408: it converts the file to a WAV (`ffmpeg`), checks the exact length of
the WAV against the same limit, stores the WAV and answers. An upload that began with the flag off stores ONE WAV for Azure, so both
of its checks (the header and the WAV) are capped at Azure's 240 minutes whatever the flags say by then: if both are turned on while
it is in flight, a 5 hour recording is still refused rather than stored as a file Azure fails. This is the path that can still reach the gateway's 30 s for a long
recording that is within the limit.

**Flag on.** No request converts anything:

1. `POST /api/v1/uploads`, after the header check, stores the **original** as
   `incoming/{user_id}/{upload_id}{ext}` (blocks sent `BLOB_UPLOAD_CONCURRENCY` = 8 at a time) and answers 201 as before:
   `blob_name` is still the WAV's future name under `uploads/{user_id}/`, so the ownership checks are unchanged.
2. `POST /api/v1/jobs` finds that original and creates the job `PENDING` with `source_blob_path` set and **no Azure job**: it
   does not call Azure Speech (below, for what it does whatever the flag). The job's own WAV is named from the job as well as the
   upload: `uploads/{user_id}/{upload_id}-{job_id}-{stem}.wav` (`job_wav_name`), still under `uploads/{user_id}/` for the ownership
   check and still starting with the upload's ID, and its part blobs are derived from that name, so no two jobs share a blob.
3. The worker's third task, `PreparationService` (`stt/preparation_service.py`, beside the batch poller and the dictation poller),
   claims such jobs, converts the original (`normalize_to_wav`), checks the exact length of the WAV, stores it, submits the job
   through the same code as a request (`submit_existing_job`) and deletes the original. A job that cannot be converted or is too
   long by the exact check becomes `FAILED` with the sentence the upload gives for the same file (the web already shows
   `error_message`); a storage failure is retried `PREPARE_MAX_ATTEMPTS` times and then fails the job; a crash or a restart
   leaves the original and the job `PENDING`, and the job is claimed again once its lease (`PREPARE_LEASE_SECONDS`, renewed every
   `PREPARE_HEARTBEAT_SECONDS`) has run out. A worker that is stopping (or any request or task that is cancelled) kills the `ffmpeg`
   or `ffprobe` it is running and waits for it to exit (`stt/subprocesses.py`) before it lets go of the job and removes its temporary
   directory, so no conversion goes on writing behind it (the kill is sent before the call can be left, and a killed process runs no more
   code, so a further cancellation while it is reaped cannot leave it writing; the reaping carries on shielded). The file writes the worker and the local (development) storage hand to a thread (the copy of
   an original into the temporary directory, the store and delete of a local blob, the download of an Azure blob to a file, removing
   the original from the temporary directory) are waited for in the same way (`stt/uninterrupted.py`, `run_to_completion`): a
   cancelled task raises its cancellation only after the thread has finished, so a copy cannot outlive the lease or the directory,
   or recreate a blob after cleanup. A job is `PENDING` throughout; no new status exists.

**`POST /jobs`, whatever the flag.** It looks for the upload's original by the user's and the upload's ids (never by anything the
client sends), whatever the flag says now, so an upload made while the flag was on is still deferred if the flag is turned off
before its `POST /jobs`. Found: the job is created `PENDING` as above. Not found (the upload was converted in the request): the
job is submitted at once, as before. Only a lookup that succeeds and finds nothing
submits at once. If the lookup itself fails, the request is a 502 ("The uploaded recording could not be checked. Please try
again.") and no job is made, **whatever the flag**: a failure does not say there is no original, and after a rollback the WAV
does not exist, so submitting would send Azure a missing blob. With the flag off this is one extra lookup in storage per
`POST /jobs`, and a storage outage now fails job creation (as it fails the upload) where before DIAAT-408 it did not. The worker starts the preparation service with the other pollers and does not look at the flag to decide
what to prepare: it prepares whatever job is waiting (only its length check reads the flags, as the effective limit).

| Setting | Default | Meaning |
|---|---|---|
| `PREPARE_POLL_INTERVAL_SECONDS` | 5 | How often the worker looks for jobs to prepare. |
| `PREPARE_MAX_CONCURRENT` | 2 | Jobs prepared at once. Each needs temporary disk for the original and the WAV (up to about 1.5 GB for a 1 GiB upload); the worker pod has no ephemeral-storage limit (the shared Helm chart only sets memory and CPU), so this is node disk. |
| `PREPARE_MAX_ATTEMPTS` | 3 | Attempts before a job fails. A crashed attempt counts. |
| `PREPARE_LEASE_SECONDS` / `PREPARE_HEARTBEAT_SECONDS` | 300 / 30 | A job being prepared is renewed every heartbeat; another attempt may take it over once the lease has run out. The lease must be at least twice the heartbeat. |
| `PREPARE_RETRY_BACKOFF_SECONDS` | 30 | Wait before a failed attempt is repeated (times the attempt number). |
| `BLOB_UPLOAD_CONCURRENCY` | 8 | Parallel blocks when storing the original or the WAV (memory: that many 4 MiB blocks). |

Rollback: turn the flag off (admin > Feature flags, no deploy). Jobs already deferred are still prepared by the worker, and so
is the job of an upload made with the flag on whose `POST /jobs` arrives after the flag was turned off. New uploads are
converted in the request again, after the header check. The `incoming/` prefix holds originals no job claimed if a user uploads
and never submits; the worker deletes every other original itself (when the job is prepared, fails or is deleted). A storage
lifecycle rule on the prefix would be a safety net for the abandoned ones, and is not in this repository (see the rollout notes).

## Recordings over 240 minutes (`long_recordings_enabled`, with `async_upload_preparation`)

Azure fails a file over 240 minutes, so with **both flags on** a recording longer than that (up to `MAX_AUDIO_DURATION_SECONDS`,
20 hours) is cut into parts by the worker, and each part is its own Azure batch job. A recording of 240 minutes or less is never cut:
it keeps the single-file path above, with no part rows. With either flag off the limit is 240 minutes, and a longer recording is
refused with the 4-hour sentence: by the upload from its header (or, if the header states no length, by the exact check after
conversion), or by the worker for a job that was deferred.

- **Cutting** (`stt/splitter.py`): `n = ceil(duration / PART_TARGET_SECONDS)` parts of equal length are aimed for; each boundary
  moves to the longest silence within about 10 minutes of its ideal point (ffmpeg `silencedetect`), or is cut at the ideal point if
  there is none (`cut_kind` on the part row says which). The plan is checked: contiguous, covering the whole recording, no part over
  14,400 s. The WAV is cut in Python: its PCM frames are copied
  into the parts, in a thread, at the frame nearest each planned cut (no re-encode, exact to the sample; about a third of a second
  per hour of audio on a development machine with the file cached, memory 128 KiB). Nothing goes through a command line: ffmpeg's
  segment muxer, used before, took every cut time in ONE argument, and Linux limits one argument to 128 KiB, so a long recording cut
  into short parts (`PART_TARGET_SECONDS` is valid down to 1: 71,999 cuts for 20 hours, about 0.9 MB) could never even be started, and
  the muxer cut at packet boundaries that differ between ffmpeg versions (about 0.06 s with 8.0.1, up to 0.128 s with the 9.0.2 of
  the production image). The parts are numbered `part-000.wav` upward, in cutting order, and each part's offset and length are still
  measured from its file. The planner keeps every part a second inside the limit (`SEGMENT_SLACK_SECONDS`, a margin that was sized
  for those packets and is kept) and cuts in the middle of a silence of at least half a second. A cancelled preparation stops the
  copy within a chunk and waits for the thread to end before the cut raises, however often the task is cancelled meanwhile (a
  further cancellation is remembered and raised after the wait), so the temporary directory is never removed under a writing thread.
- **One job at a time from an upload, and blobs that are never shared.** Every blob a job uses is named from its upload, and
  `blob_name` is the only thing `POST /jobs` is given, so two jobs from one upload used to share them: a converted upload's WAV
  (on master too: deleting one job deleted the other's audio), and, with this feature, the original and the worker's WAV and parts
  (one job's submission overwrote, and its cleanup, failure or delete removed, the other's, and the first job to finish deleted the
  original the second was still waiting for). Now: (1) `POST /jobs` answers **409** ("A job has already been made from this
  upload...") when the user already has a job from the upload that has not failed; a **failed** job does not count, so submitting the
  upload again is still how a job that Azure refused is retried (a deferred job that failed has lost its original: that upload is
  uploaded again); an idempotent resubmission (same key) still returns the same job; (2) a deferred job's WAV and parts carry the job's id,
  as above (jobs made before this release keep the names they have); (3) a job's delete never removes a WAV another job still records
  (a failed job and its retry from one converted upload share the WAV), nor an original another job is still `PENDING` on, and the
  worker keeps the original until no other job needs it; (4) cleanup only ever deletes a part blob derived from the job's own WAV
  name. The check in (1) is race-free on PostgreSQL: `POST /jobs` first takes `pg_advisory_xact_lock` on (user, upload)
  (`lock_upload_for_submission`), held in the same transaction until the job row is committed, so a second request for the same
  upload waits and then finds the job (409); one that cannot get the lock within 15 s is refused with a 409 "Concurrent submission
  conflict; retry". Tested on a real Postgres with two clients posting at the same moment, many times (one 201, one 409, one job, one
  Azure submission). What it does **not** cover: it is a lock, not a constraint (no unique index, which would not build where
  production already holds several jobs for one upload), so anything that creates jobs without going through `POST /jobs` is not
  held to it; on another database (SQLite, the unit tests) it does nothing; the key is hashed to 64 bits, so unrelated uploads
  may rarely wait for each other; and the lock is held while the request looks for the original in storage. Should it ever fail,
  the jobs have blobs of their own and share only the original, which (3) protects.
- **Recording the parts**: one row per part in `transcription_job_part` (migration 0018): offset in the recording, length, cut kind,
  blob (`uploads/{user_id}/{upload_id}-{job_id}-{name}.partNN.wav`, derived from the job's own WAV name, never client-supplied), Azure job,
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
- **Deleting a job** (`DELETE /jobs/{id}`) deletes its WAV, its original, every part blob and Azure job, and then the job and its
  rows. Nothing is orphaned behind a deleted row: a genuine failure to delete the WAV, the original, a part blob or a part's Azure
  job is a **502 and the job is kept** (a blob that is already gone is success), with the part rows still recording what is left
  and the job marked `needs_cleanup`, so the delete can simply be repeated. (The job's own Azure job, the one of a single-file
  job, is still deleted on a best-effort basis and never blocks the delete.) The rows are removed only after every delete has
  succeeded. A part's references are forgotten only if they still hold what was seen and deleted (compare and clear, field by
  field): a blob or Azure job the worker records while a delete is being awaited stays recorded for the next pass. The last step is one transaction that locks the job row (`SELECT ... FOR UPDATE`), deletes the part rows and reads
  what they recorded: if the worker created or recorded a part after the resources were released, the transaction is rolled back,
  that part's blob and Azure job are deleted and the step is tried again (3 times at most, then a 502 to be repeated). A worker
  that inserts a part after the delete committed fails the foreign key (checked on Postgres: it waits on the job's lock, then gets
  SQLSTATE 23503); it reads that as "the job is gone" and deletes what it made, as it does when it finds its row gone at a later
  write, and stops.
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

Temporary disk for a recording in parts: the converted WAV and its parts (about 115 MB per hour each, so about 4.6 GB for 20 hours)
per job being prepared, beside the original while it is downloaded.

## Long recordings: rollout, rollback and operation (DIAAT-408)

**Why the background preparation exists.** The CFT frontend Application Gateway gives a backend 30 seconds to answer (setting
`benchnotes`, read on AAT and demo; on prod the gateway module's default of 30 s applies, as no override is set for it in the
platform's Terraform, but the live prod gateway has not been read). Converting a recording inside `POST /uploads` takes about 13-14 s per hour of audio (measured from AAT telemetry), so a
recording of about two hours or more got a 504 although the server finished. With `async_upload_preparation` on, every request the
browser makes answers in seconds whatever the recording's length, up to the size backstop.

**The two flags and every setting.** Both flags are system feature flags (admin > Feature flags, no deploy), default off
(`ASYNC_UPLOAD_PREPARATION`, `LONG_RECORDINGS_ENABLED` set the defaults). Long recordings take effect only with both on.

| Setting | Default | What it costs, or what to know |
|---|---|---|
| `MAX_UPLOAD_BYTES` | 1073741824 (1 GiB) | The backstop on one upload. **The web pod's `MAX_UPLOAD_BYTES` environment variable must be the same number** (the web refuses a declared size over its own before any of the body is read). 1 GiB is the agreed limit for now. One request through the gateway chain is untested above it, and a larger figure would also need an upload in blocks that can resume (not built, no follow-up planned). |
| `MAX_AUDIO_DURATION_SECONDS` | 72000 (20 h) | Applies in full only with both flags on; otherwise the limit is `min(this, 14400)`. |
| `PART_TARGET_SECONDS` | 12600 (3 h 30 m) | Longest part aimed for; 1 to 14400. |
| `MAX_PARALLEL_PARTS` | 4 | Parts stored and submitted at once: that many Azure batch jobs at once for one recording. Azure's concurrency quota for the Speech resource was not looked up (`SPEECH_CONCURRENT_JOBS_LIMIT` only drives a utilisation gauge and throttles nothing). |
| `BLOB_UPLOAD_CONCURRENCY` | 8 | Blocks sent in parallel per blob: 8 x 4 MiB of memory per upload in flight (API pod: the original, 512 Mi limit; worker: per part and for the full WAV). Only the asynchronous path uses it. |
| `PREPARE_POLL_INTERVAL_SECONDS` | 5 | How often the worker looks for jobs to prepare. |
| `PREPARE_MAX_CONCURRENT` | 2 | Jobs prepared at once by one worker: each needs temporary disk for its WAV and parts (about 115 MB per hour of audio each, so about 2 x 2.3 GB for 20 h, plus the original while it is downloaded and converted, up to `MAX_UPLOAD_BYTES`). The worker pod has no ephemeral-storage limit (the shared Helm chart only sets memory and CPU), so this is node disk. |
| `PREPARE_MAX_ATTEMPTS` | 3 | Attempts at a job before it fails (a crashed attempt counts). |
| `PREPARE_LEASE_SECONDS` / `PREPARE_HEARTBEAT_SECONDS` | 300 / 30 | A job being prepared is renewed every heartbeat; another worker may take it over once the lease has run out. A crash is therefore retried after up to 5 minutes. The lease must be at least twice the heartbeat. |
| `PREPARE_RETRY_BACKOFF_SECONDS` | 30 | Wait before a failed attempt is repeated (times the attempt number). |
| `PART_MAX_ATTEMPTS` / `PART_RETRY_BACKOFF_SECONDS` | 3 / 5 | Attempts at one part before the job fails, and the wait between them. |
| `JOB_STUCK_PART_FACTOR` | 1.5 | A job in parts is reported stuck after this many times its longest part has run (single-file jobs keep `JOB_STUCK_RUNNING_SECONDS`, 6 h). |

**Rollout order.** (1) Deploy the API, the worker and the web with both flags off: migrations 0017 and 0018 are additive and run at
startup, the previous release's pods keep working while it rolls out, and behaviour is as before except that an upload's length is read
from its header first and `POST /jobs` looks in storage for a waiting original (finding none). (2) Ask the platform team
for the two infrastructure items below. (3) On AAT turn on `async_upload_preparation`, run part A of the checklist below. (4) On AAT
turn on `long_recordings_enabled`, run part B. (5) Repeat on prod once AAT has passed, with a few users first.

**Rollback.** Turn the flags off (no deploy). Long recordings go first: with `long_recordings_enabled` off a job already in parts
carries on (its worker and poller do not look at the flag), but a recording over 240 minutes uploaded later is refused. With
`async_upload_preparation` off, jobs already waiting for preparation are still prepared by the worker, and a `POST /jobs` for an
upload made while it was on still creates a waiting job (see "How an upload is checked"). A job being prepared when the flag is
turned off is not affected. The one thing a rollback fails is a job whose preparation needs the effective limit: it is checked
against it when the worker converts it, and the limit is 240 minutes unless both flags are on, so a recording over 240 minutes
still waiting when either `long_recordings_enabled` or `async_upload_preparation` is turned off fails with the 4-hour sentence.

**Infrastructure (not in this repository).** (a) The worker's temporary files are on the node's disk: the shared Helm chart sets
only memory and CPU, so no ephemeral-storage limit can be set from this repository, and the AKS nodes use ephemeral OS disks of a
size that depends on the VM size, which is not recorded here. Check what disk the nodes give the worker, or watch the worker's
restarts during the long-recording tests on AAT; a node under disk pressure evicts the worker, and the job is prepared again from the
start. (b) A lifecycle rule on the `incoming/` prefix of the recordings storage account that deletes blobs after a day is a safety
net for originals of uploads that never became a job (the worker deletes all others). The storage module's `policy` option can
only delete blob versions, so the rule would be an `azurerm_storage_management_policy` resource in the shared-infrastructure
repository. (c) The live prod Application Gateway's 30 s backend timeout has not been read (see above).

**Upload limit.** `MAX_UPLOAD_BYTES` is one setting read by the API and the web, and 1 GiB is the agreed limit for now. A larger
one would also need an upload in blocks (start, block and complete calls, resumable after a drop); that is not built and no
follow-up is planned. One request through Front Door, Traefik and Caddy has not been tried above 1 GiB.

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
- A file whose header states no length, or whose estimate or tolerance puts it near the limit, passes the header check, whatever the flag. With the asynchronous path on it is accepted
  by the upload and a bad one fails as a job; with the flag off it is converted inside the request first (slow for a long one)
  and then checked exactly. Observed with ffmpeg 8.0.1 (the production image has 9.0.2, which was not run locally, so this is unconfirmed there): text named `.mp3` makes `ffprobe` exit 0 and report no duration (its MP3
  detection is lenient), so the header cannot refuse it and the conversion does (flag off: a 422 in the request; flag on: the job
  fails with "The audio file could not be decoded..."). A file `ffprobe` refuses is a 422 in the request either way. The length
  of a VBR MP3 with no Xing header is only an estimate (see above); the exact check on the converted WAV is the backstop.
- Nothing here has been run against real Azure Speech or Blob Storage.

**Measured locally (not on a pod, not with real hearings).** On a development machine (8 cores, ffmpeg 8.0.1; the production image has 9.0.2, so every figure here, and every ffmpeg behaviour described above as observed, was measured on 8.0.1 and not re-measured on 9.0.2), with synthetic
8 kHz mono 8 kbps MP3 tone files (3.6 MB per hour; real recordings decode more slowly): an 8 h file (28.8 MB) converted to the 16 kHz
mono WAV (922 MB) in about 10-16 s, silence detection over that WAV took about 11 s and cutting it into three parts about 4-5 s;
ffmpeg's own peak memory was about 55 MB in each step. The browser-facing requests, with the real routes over local storage,
answered in 0.35 s (`/uploads`, 3.6 MB) to 0.9 s (28.8 MB) and 0.03-0.13 s (`/jobs`). The heavy end-to-end test (opt-in, below) ran
in 4 min 28 s with a peak of 507 MB resident for the whole test process (the API, the services and the test; ffmpeg is a separate
process). AAT measured 13-14 s per hour of conversion on a pod for real uploads: expect the preparation of an 8 h recording to take
about 2 minutes there and a 20 h recording about 4.5 to 5 minutes of conversion before cutting, and size the worker's disk from the table above, not from these figures.

**The end-to-end test.** `tests/unit/recording/long_e2e/` runs the real routes, the preparation service, the poller and real
ffmpeg with the mock Speech backend over a 1 h, a 4 h (the limit), a 5 h and an 8 h recording, the three refusals (4 h 01 m with both
flags off, with background preparation on and long recordings off, and 8 h 02 m), a corrupt file and a job deleted while its parts
are in flight. Every scenario runs in the normal suite at a scaled-down size (30 s stands for 240 minutes); the real sizes are
deselected unless `RUN_LONG_E2E=1` (`pytest tests/unit/recording/long_e2e -m long_e2e`, optionally `LONG_E2E_DIR` for the generated
audio and `LONG_E2E_REPORT` for a JSON of the timings; about 4 GB of temporary disk and 5 minutes).

## Operator checklist for the AAT validation (NOT YET EXECUTED)

Nothing below has been run: it is what to do after the first AAT deploy. Record the browser's time for each request (developer
tools), the job's status over time, and the App Insights events (`customEvents`, filtered on `hmcts.journey.id` = the job id).

**Before:** both flags off; the web pod's `MAX_UPLOAD_BYTES` unset or equal to the API's; the infrastructure asks above in place or
the risk accepted; `GET /api/flags` shows `max_recording_seconds: 14400`.

**Part A, `async_upload_preparation` on** (long off): `max_recording_seconds` is still 14400.

| File | Expected |
|---|---|
| `bad.mp3` (not audio) | A 422 "could not be decoded" in under 2 s, or (if `ffprobe` is lenient with it, as it was locally) a 201 and then a failed job with the same sentence. |
| `over.mp3` (4 h 01 m), `just-over.mp3` | A 422 with the 4-hour sentence in under 10 s, nothing stored. (A VBR MP3 without a Xing header is judged by an estimate: one only a little over the limit is converted first and refused after, check each way. A file within 2 s of the limit is converted and judged exactly.) |
| `timing-60min.mp3`, `timing-75min`, `timing-120min.mp3` | 201 from `/uploads` and `/jobs` each in under 10 s, job `pending` ("being prepared"), then `processing`, then `completed`; no part rows. |
| `timing-180min.mp3`, `timing-B-180min-32kbps.mp3`, `under-3h59m.mp3` | The same; previously a 504 and a duplicate job. |
| `exact-240.mp3` (14,400 s) | Accepted, one Azure job, no part rows. What Azure does at exactly 240 minutes is unknown: record it. |
| `wav-near-cap.wav` (988 MB) | `/uploads` answers inside the gateway limit, or `MAX_UPLOAD_BYTES` (web and API) must come down. Not measured anywhere yet. |
| A job deleted while `pending` | No original left in `incoming/`; no WAV. |
| Flip the flag off with an upload in flight | Jobs already deferred still complete; an upload made with the flag on whose `/jobs` lands after the flip still gives a `pending` job that the worker prepares and submits. |
| Telemetry | `preparation.started/completed` per job, no 504s, server time per request under 10 s; the stuck gauge does not trip for preparing jobs. |

**Part B, `long_recordings_enabled` on as well** (`max_recording_seconds` 72000):

| File | Expected |
|---|---|
| `over.mp3` (4 h 01 m) | Accepted; two parts; completes. |
| A 5 h recording | Two parts; the transcript's second half starts at the second part's offset (about 2.5 h), times monotonic; speakers labelled `Part 1 · Speaker N`, `Part 2 · Speaker N`; one `job.finished` with `job.part_count` 2. |
| An 8 h recording | Three parts, each at most 4 h (about 2 h 40 m with the defaults); record the wall clock of conversion, `splitting.completed`, each part's `job.part_finished` and the whole job; the worker pod's memory and disk while it prepares (`kubectl top` and ephemeral storage). |
| A 20 h recording (it needs a file under 1 GiB: at most about 119 kbps, e.g. a 64 kbps MP3 is about 576 MB) | Six parts of about 3 h 20 m, four prepared and submitted at once, two waves; record the wall clock of conversion, of the splitting and of each part, and the worker pod's memory and disk (about 4.6 GB of temporary disk for the WAV and its parts). |
| A 20 h 02 m recording | A 422 with the 20-hour sentence in under 10 s. |
| Speaker rename across parts | In the job page, rename `Part 1 · Speaker 0` and `Part 2 · Speaker 0` to the same name with "Replace all": one speaker. |
| A job deleted mid-parts | No part blob, Azure job or row left. |
| A part that fails (if one can be provoked) | The job fails at once naming the part; the others' Azure jobs and blobs are deleted. |
| Rollback | Turn `long_recordings_enabled` off: a job in parts finishes; a new 5 h upload is refused with the 4-hour sentence. |
| Both flags off | `over.mp3` is refused in seconds from its header with the 4-hour sentence, nothing converted or stored. A recording within the limit is still converted inside the request (slow: seconds per hour of audio), as before. |

Also record: Azure's behaviour for parts of about 2 h 40 m and 3 h 30 m (time and cost), whether `silencedetect` found a silence
in each cut window on real hearings (`split.forced_cut_count`), and whether `MAX_PARALLEL_PARTS` 4 hits an Azure concurrency limit.
