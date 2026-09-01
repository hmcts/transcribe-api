#!/usr/bin/env bash
# Reproducible import of code from the upstream repos' main branches.
# Reads only; never writes to the source repos.
set -euo pipefail
SRC="$1"; T="$(cd "$(dirname "$0")" && pwd)/src/transcribe_api"
B="$SRC/batch-audio-transcription/src/transcription_svc"
C="$SRC/courtstranscribe/backend"

rm -rf "$T"
mkdir -p "$T"/{stt,realtime,documents/{llm,minutes,lib},domain/{auth,notify},runtime,api}

# ---------- runtime: cross-cutting leaves (settings, db, blob, logging, security) ----------
cp "$B/config/settings.py"        "$T/runtime/settings_recording.py"
cp "$C/utils/settings.py"         "$T/runtime/settings_dictation.py"
cp "$B/database/engine.py"        "$T/runtime/db_recording.py"
cp "$C/app/database/postgres_database.py" "$T/runtime/db_dictation.py"
cp "$B/audio/azure_utils.py"      "$T/runtime/blob_recording.py"
cp "$C/app/audio/azure_utils.py"  "$T/runtime/blob_dictation.py"
cp "$B/auth/validators.py"        "$T/runtime/security.py"
cp "$C/app/logger.py"             "$T/runtime/logger.py"
for f in cors_utils exception_handlers middleware log_sanitization version markdown; do
  [ -f "$C/utils/$f.py" ] && cp "$C/utils/$f.py" "$T/runtime/$f.py"
done

# ---------- stt: capability A + C ----------
for f in batch_client submission polling_service preprocessing accuracy wer local_storage; do
  cp "$B/audio/$f.py" "$T/stt/$f.py"
done
cp "$B/audio/speakers.py"                        "$T/stt/speakers_recording.py"
cp "$C/app/audio/speakers.py"                    "$T/stt/speakers_dictation.py"
cp "$C/app/audio/transcription.py"               "$T/stt/fast_client.py"
cp "$C/app/audio/process_audio_fully.py"         "$T/stt/pipeline.py"
cp "$C/app/audio/transcription_polling_service.py" "$T/stt/work_poller.py"
cp "$C/app/audio/utils.py"                       "$T/stt/utils.py"

# ---------- domain: capability F ----------
cp "$B/database/models.py"                  "$T/domain/models_recording.py"
cp "$B/database/interface.py"               "$T/domain/interface_recording.py"
cp "$C/app/database/postgres_models.py"     "$T/domain/models_dictation.py"
cp "$C/app/database/interface_functions.py" "$T/domain/interface_dictation.py"
cp "$C/app/database/audit.py"               "$T/domain/audit.py"
cp "$C/app/database/connection.py"          "$T/runtime/db_connection.py"
cp "$C/app/database/exceptions.py"          "$T/domain/exceptions.py"
cp "$C/utils/langfuse_models.py"            "$T/documents/llm/langfuse_models.py"
cp "$B/webhook/dispatcher.py"               "$T/domain/webhook.py"
# auth: BOTH services now use the shared Entra library on main
for f in approles auth_models dependencies; do
  cp "$B/utils/$f.py" "$T/domain/auth/${f}_recording.py"
  cp "$C/utils/$f.py" "$T/domain/auth/${f}_dictation.py"
done
cp "$C/utils/jwt_verification.py" "$T/domain/auth/jwt_verification.py"
for f in email_utils gov_notify; do cp "$C/utils/$f.py" "$T/domain/notify/$f.py"; done

# ---------- documents: capability D + E ----------
cp "$C/app/documents/"*.py "$T/documents/"
cp "$C/app/llm/"*.py       "$T/documents/llm/"
cp "$C/app/llm/"*.toml     "$T/documents/llm/" 2>/dev/null || true
cp -R "$C/app/minutes/."   "$T/documents/minutes/"
cp "$C/lib/"*.py           "$T/documents/lib/"

# ---------- api ----------
cp "$B/api/routes.py"                  "$T/api/routes_recording.py"
cp "$B/api/dependencies.py"            "$T/api/deps_recording.py"
cp "$B/api/app.py"                     "$T/api/app_recording.ref"
cp "$C/api/routes.py"                  "$T/api/routes_dictation.py"
cp "$C/api/privileged_routes.py"       "$T/api/routes_dictation_privileged.py"
cp "$C/api/document_content_models.py" "$T/api/document_content_models.py"
cp "$C/main.py"                         "$T/api/app_dictation.ref"

# ---------- build utils (imported by tests) ----------
mkdir -p "$(dirname "$T")/../build_utils"
cp "$C/build_utils/"*.py "$(dirname "$T")/../build_utils/"
touch "$(dirname "$T")/../build_utils/__init__.py"

# ---------- migrations, tests, vendored wheel ----------
R="$(dirname "$T")/.."
rm -rf "$R/alembic" "$R/tests" "$R/vendor"
cp -R "$C/alembic" "$R/alembic"; cp "$C/alembic.ini" "$R/alembic.ini"
# recording has its OWN alembic history (13 revisions) — preserved separately
# until the two histories are consolidated. See architecture risks.
rm -rf "$R/alembic_recording"
cp -R "$SRC/batch-audio-transcription/migrations" "$R/alembic_recording"
mkdir -p "$R/tests/recording" "$R/tests/dictation"
cp -R "$SRC/batch-audio-transcription/tests/." "$R/tests/recording/"
cp -R "$C/tests/." "$R/tests/dictation/"
find "$R/tests" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
