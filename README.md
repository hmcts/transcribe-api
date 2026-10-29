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

## Branches and environments

- Feature branches start from `develop`, and their PRs go into `develop`. Each PR gets its own preview.
- `develop` deploys to demo (`benchnotes.demo.platform.hmcts.net`). The permanent draft PR #45 (`develop` → `master`) rebuilds on every push to `develop`, and Flux deploys its `pr-45-*` image. Never merge, close or mark #45 ready.
- To release, open a PR into `master` from a branch cut from `develop`. `master` deploys to AAT, then prod.
- Merge `master` into `develop` at least every 2 weeks, by a PR from a branch cut from `master` (not from `master` itself: Jenkins stops building a branch that is the head of an open PR), with a merge commit. Old `pr-*` images expire after about 3 weeks, and demo pods then fail to pull.
- Migrations go only through `master`. A feature that needs schema first lands an additive migration on `master` by its own PR; then `master` is merged into `develop`, then the feature. A revision that exists only on `develop` gives Alembic two heads at the next merge.
- `demo` is the CNP branch for the demo Terraform only, and every `master` build overwrites it. Demo uses `master`'s Helm chart, so demo-only settings go in cnp-flux-config `apps/transcribe/transcribe-api/demo.yaml`.

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
