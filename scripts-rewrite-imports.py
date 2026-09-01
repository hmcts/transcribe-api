#!/usr/bin/env python3
"""Rewrite upstream import paths onto the merged module layout.

Run after scripts-import.sh. Idempotent. Longest prefixes first so that e.g.
`app.audio.azure_utils` is matched before `app.audio`.
"""
from __future__ import annotations

import pathlib
import re
import sys

P = "transcribe_api"

# (old dotted prefix, new dotted prefix) — order matters, longest first.
MAP: list[tuple[str, str]] = [
    # ---------- recording service (transcription_svc.*) ----------
    ("transcription_svc.config.settings", f"{P}.runtime.settings_recording"),
    ("transcription_svc.database.engine", f"{P}.runtime.db_recording"),
    ("transcription_svc.database.models", f"{P}.domain.models_recording"),
    ("transcription_svc.database.interface", f"{P}.domain.interface_recording"),
    ("transcription_svc.audio.azure_utils", f"{P}.runtime.blob_recording"),
    ("transcription_svc.audio.speakers", f"{P}.stt.speakers_recording"),
    ("transcription_svc.audio", f"{P}.stt"),
    ("transcription_svc.auth.validators", f"{P}.runtime.security"),
    ("transcription_svc.webhook.dispatcher", f"{P}.domain.webhook"),
    ("transcription_svc.utils.approles", f"{P}.domain.auth.approles_recording"),
    ("transcription_svc.utils.auth_models", f"{P}.domain.auth.auth_models_recording"),
    ("transcription_svc.utils.dependencies", f"{P}.domain.auth.dependencies_recording"),
    ("transcription_svc.api.dependencies", f"{P}.api.deps_recording"),
    ("transcription_svc.api.routes", f"{P}.api.routes_recording"),
    ("transcription_svc.api.app", f"{P}.api.app"),
    ("transcription_svc", P),
    # ---------- dictation service (app.*, utils.*, lib.*, api.*) ----------
    ("app.audio.azure_utils", f"{P}.runtime.blob_dictation"),
    ("app.audio.speakers", f"{P}.stt.speakers_dictation"),
    ("app.audio.transcription_polling_service", f"{P}.stt.work_poller"),
    ("app.audio.process_audio_fully", f"{P}.stt.pipeline"),
    ("app.audio.transcription", f"{P}.stt.fast_client"),
    ("app.audio.utils", f"{P}.stt.utils"),
    ("app.audio", f"{P}.stt"),
    ("app.database.postgres_models", f"{P}.domain.models_dictation"),
    ("app.database.postgres_database", f"{P}.runtime.db_dictation"),
    ("app.database.interface_functions", f"{P}.domain.interface_dictation"),
    ("app.database.audit", f"{P}.domain.audit"),
    ("app.database.connection", f"{P}.runtime.db_connection"),
    ("app.database.exceptions", f"{P}.domain.exceptions"),
    ("utils.langfuse_models", f"{P}.documents.llm.langfuse_models"),
    ("app.documents", f"{P}.documents"),
    ("app.llm", f"{P}.documents.llm"),
    ("app.minutes", f"{P}.documents.minutes"),
    ("app.logger", f"{P}.runtime.logger"),
    ("utils.jwt_verification", f"{P}.domain.auth.jwt_verification"),
    ("utils.approles", f"{P}.domain.auth.approles_dictation"),
    ("utils.auth_models", f"{P}.domain.auth.auth_models_dictation"),
    ("utils.dependencies", f"{P}.domain.auth.dependencies_dictation"),
    ("utils.email_utils", f"{P}.domain.notify.email_utils"),
    ("utils.gov_notify", f"{P}.domain.notify.gov_notify"),
    ("utils.settings", f"{P}.runtime.settings_dictation"),
    ("utils.cors_utils", f"{P}.runtime.cors_utils"),
    ("utils.exception_handlers", f"{P}.runtime.exception_handlers"),
    ("utils.log_sanitization", f"{P}.runtime.log_sanitization"),
    ("utils.middleware", f"{P}.runtime.middleware"),
    ("utils.markdown", f"{P}.runtime.markdown"),
    ("utils.version", f"{P}.runtime.version"),
    ("api.document_content_models", f"{P}.api.document_content_models"),
    ("api.privileged_routes", f"{P}.api.routes_dictation_privileged"),
    ("api.routes", f"{P}.api.routes_dictation"),
    ("lib.", f"{P}.documents.lib."),
    ("backend.app", f"{P}"),
]


def rewrite(text: str) -> tuple[str, int]:
    n = 0
    for old, new in MAP:
        # `from X import`, `import X`, and string references used by mock.patch
        for pat, repl in (
            (rf"(\bfrom\s+){re.escape(old)}(\b)", rf"\1{new}\2"),
            (rf"(\bimport\s+){re.escape(old)}(\b)", rf"\1{new}\2"),
            (rf'(["\']){re.escape(old)}(\.)', rf"\1{new}\2"),
            # attribute-style usage after a plain `import X`, e.g. api.routes.router
            (rf"(?<![.\w]){re.escape(old)}(\.\w)", rf"{new}\1"),
        ):
            text, k = re.subn(pat, repl, text)
            n += k
    return text, n


def main() -> int:
    root = pathlib.Path(__file__).parent
    targets = list((root / "src").rglob("*.py")) + list((root / "tests").rglob("*.py"))
    total_files = total_subs = 0
    for f in targets:
        original = f.read_text()
        updated, n = rewrite(original)
        if n:
            f.write_text(updated)
            total_files += 1
            total_subs += n
    # every package dir needs __init__.py
    created = 0
    for d in (root / "src" / P).rglob("*"):
        if d.is_dir() and not (d / "__init__.py").exists():
            (d / "__init__.py").write_text("")
            created += 1
    if not (root / "src" / P / "__init__.py").exists():
        (root / "src" / P / "__init__.py").write_text("")
        created += 1
    print(f"rewrote {total_subs} import references across {total_files} files")
    print(f"created {created} missing __init__.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
