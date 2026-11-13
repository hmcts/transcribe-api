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
