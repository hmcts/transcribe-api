"""Conftest for the merged unit suite.

Scoped to tests/unit deliberately: tests/smoke and tests/functional run against
a deployed environment over HTTP and must not have a local settings environment
pinned underneath them.

Two problems arise from merging two suites into one process, and both are fixed
here rather than in either suite.

1. Memoised settings. Both `get_settings()` implementations are cached (`@cache`
   on the dictation side, `@lru_cache` on the recording side). Whichever suite
   ran first populated the cache and the other then saw the wrong settings, so
   results were order-dependent: dictation-first broke four recording auth
   tests, recording-first broke a dictation settings test.

2. Leakage from a developer's `.env`. Both settings classes declare
   `env_file=".env"`. Upstream the recording service had no `.env`, so its tests
   ran on declared defaults. This repo needs a `.env` to run the app locally,
   which silently changed test behaviour. Real environment variables take
   precedence over `env_file` in pydantic-settings, so setting them explicitly
   here makes the suite independent of whatever `.env` happens to contain.
"""

from __future__ import annotations

import os

import pytest

# A complete, inert test environment. Values are fake by design; nothing here
# should reach a real service.
TEST_ENV: dict[str, str] = {
    "ENVIRONMENT": "test",
    "APP_URL": "http://localhost:8000",
    "AZURE_AD_CLIENT_ID": "test-client-id",
    "AZURE_AD_TENANT_ID": "test-tenant-id",
    "AZURE_GROK_API_KEY": "test-grok-key",
    "AZURE_GROK_ENDPOINT": "https://test.openai.azure.com/",
    "AZURE_OPENAI_API_KEY": "test-openai-key",
    "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com/",
    "AZURE_SPEECH_KEY": "test-speech-key",
    "AZURE_SPEECH_REGION": "eastus",
    "AZURE_SPEECH_ENDPOINT": "https://test.cognitiveservices.azure.com",
    "AZURE_STORAGE_ACCOUNT_NAME": "teststorage",
    "AZURE_STORAGE_CONTAINER_NAME": "test-container",
    "AZURE_STORAGE_TRANSCRIPTION_CONTAINER": "test-transcription-container",
    "AZURE_STORAGE_CONNECTION_STRING": "",
    # Pinned so a developer's local .env (AUDIO_STORAGE_BACKEND=local,
    # SPEECH_BACKEND=mock — see runtime/settings_recording.py) can never
    # change what these tests actually exercise: real env vars outrank
    # env_file in pydantic-settings, but only for keys this dict sets.
    "AUDIO_STORAGE_BACKEND": "azure",
    "SPEECH_BACKEND": "azure",
    "LOCAL_AUDIO_BASE_URL": "",
    "DATABASE_CONNECTION_STRING": "postgresql://test:test@localhost:5432/test_db",
    "GOOGLE_APPLICATION_CREDENTIALS_JSON_OBJECT": "{}",
    "GOV_NOTIFY_API_KEY": "test-notify-key",
    "LANGFUSE_PUBLIC_KEY": "pk-lf-test-public-key-123456",
    "LANGFUSE_SECRET_KEY": "sk-lf-test-secret-key-123456",
    "LANGFUSE_HOST": "https://langfuse-ai.justice.gov.uk",
    "JWT_SECRET_KEY": "test-jwt-secret",
    # DIAAT-373: signs the BenchNotes access token. A fixed key for the tests only.
    "APP_JWT_SIGNING_KEY": "test-benchnotes-signing-key-0123456789abcdef",  # pragma: allowlist secret
    "APP_JWT_TTL_SECONDS": "300",
    "DISABLE_FASTAPI_INSTRUMENTATION": "true",
    "RUN_WORKERS": "false",
}


# Pin the environment at IMPORT time as well as per test.
#
# The autouse fixture below runs before each test, which is too late for some
# modules: several import code that calls get_settings() at module level, so
# settings are built during COLLECTION, before any fixture runs. On a developer
# machine that is masked by .env (LocalSettings reads it); on the CNP Jenkins
# agent there is no .env, and collection failed with "7 validation errors for
# LocalSettings" — exactly the seven required dictation fields.
#
# pytest imports a conftest.py before collecting the modules beside it, so
# setting the variables here guarantees they exist for every collection-time
# import. Real environment variables outrank env_file in pydantic-settings, so
# this also keeps the suite independent of whatever a local .env contains.
os.environ.update(TEST_ENV)

# TEST_ENV only protects keys it lists — a test that does
# monkeypatch.delenv("SOME_KEY") to exercise the "not configured" branch (e.g.
# test_local_storage.py's test_raises_when_base_url_not_configured) falls
# straight through to whatever a developer's own .env has set, since removing
# the real env var exposes the next-highest-priority source. Disabling
# env_file here for the whole test session is the only way to make the suite
# genuinely independent of local .env content rather than independent only
# for the fields TEST_ENV happens to pin.
from transcribe_api.runtime import settings_dictation, settings_recording  # noqa: E402

settings_recording.Settings.model_config["env_file"] = None
settings_dictation.LocalSettings.model_config["env_file"] = None


def _clear_settings_caches() -> None:
    from transcribe_api.runtime import settings_dictation, settings_recording

    for module in (settings_dictation, settings_recording):
        getter = getattr(module, "get_settings", None)
        cache_clear = getattr(getter, "cache_clear", None)
        if cache_clear is not None:
            cache_clear()


@pytest.fixture(autouse=True)
def isolate_test_environment(monkeypatch: pytest.MonkeyPatch):
    """Pin a known environment and stop settings leaking between suites."""
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    _clear_settings_caches()
    yield
    _clear_settings_caches()


@pytest.fixture(autouse=True)
def every_user_is_an_iac_judge(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """DIAAT-322, DIAAT-390: every request acts in the IAC (as a Judge, or as the admin role a fake
    user holds), without a database:
    the context the auth dependency would carry from the BenchNotes token, and the jurisdiction
    lookup behind it, are replaced. Tests of jurisdictions and contexts themselves mark
    themselves ``real_jurisdictions`` and keep the real ones."""
    if request.node.get_closest_marker("real_jurisdictions"):
        return
    from tests.unit.jurisdiction_fixtures import IAC_JURISDICTION_ID, iac_membership
    from transcribe_api.domain import jurisdictions
    from transcribe_api.domain.auth import context as auth_context
    from transcribe_api.domain.auth.app_token import Context

    def context_of(user):
        # An AuthenticatedUser says what its token says. A plain User a test injects stands for
        # someone in the IAC, as the role its fake ``app_roles`` name: a Judge unless they are a
        # LegalTextManager, a JurisdictionAdministrator or a JurisdictionSuperAdministrator
        # (what the tests of the admin routes mean).
        if hasattr(user, "db_user"):
            return user.ctx
        roles = getattr(user, "__dict__", {}).get("app_roles", [])
        role = next((r for r in ("JurisdictionSuperAdministrator", "JurisdictionAdministrator", "LegalTextManager") if r in roles), "Judge")
        return Context(None if role == "JurisdictionSuperAdministrator" else "IAC", role)

    def sysadmin_of(user):
        if hasattr(user, "db_user"):
            return user.sysadmin
        return "SystemAdministrator" in getattr(user, "__dict__", {}).get("app_roles", [])

    monkeypatch.setattr(auth_context, "context_of", context_of)
    monkeypatch.setattr(auth_context, "sysadmin_of", sysadmin_of)
    monkeypatch.setattr(
        jurisdictions, "_load_membership", lambda code, role: iac_membership(role) if code == "IAC" else None
    )
    monkeypatch.setattr(jurisdictions, "_assigned_jurisdiction_ids", lambda user_id: [IAC_JURISDICTION_ID])
    # Every record is in the IAC too (DIAAT-334): left to the ownership checks.
    monkeypatch.setattr(jurisdictions, "_owned_transcription_jurisdiction", lambda transcription_id, user_id: (False, None))
    monkeypatch.setattr(jurisdictions, "_job_owner_and_jurisdiction", lambda job_id: (False, None, None))
    # And every upload was made in the IAC.
    monkeypatch.setattr(jurisdictions, "_record_upload_jurisdiction", lambda upload_id, jurisdiction_id: None)
    monkeypatch.setattr(jurisdictions, "_recorded_upload_jurisdiction", lambda upload_id: iac_membership().jurisdiction_id)


@pytest.fixture(autouse=True)
def feature_flags_are_not_stored(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """DIAAT-388: no test reads the system_feature_flag table (and so connects to a database)
    from every dictation request: no flag has a row, so each has its default (the
    DICTATION_ENABLED setting). Tests of the flag layer itself mark themselves ``real_feature_flags``."""
    if request.node.get_closest_marker("real_feature_flags"):
        return
    from transcribe_api.domain import feature_flags

    monkeypatch.setattr(feature_flags, "_stored_value", lambda name: None)


@pytest.fixture
def jurisdiction_db(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """DIAAT-322: an in-memory SQLite database with the jurisdiction tables, used by the
    jurisdiction lookups, the dictation interface, the auth resolver (first sign-in)
    and the document content loader. For tests marked ``real_jurisdictions``."""
    assert request.node.get_closest_marker("real_jurisdictions"), (
        "mark the test real_jurisdictions: without it every user is an IAC judge, whatever is in this database"
    )
    from sqlalchemy.pool import StaticPool
    from sqlmodel import SQLModel, create_engine

    from tests.unit.jurisdiction_fixtures import JURISDICTION_TABLES
    from transcribe_api.documents import content_loader
    from transcribe_api.domain import audit, interface_dictation, jurisdictions
    from transcribe_api.domain.auth import dependencies_dictation
    from transcribe_api.runtime import db_connection

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=JURISDICTION_TABLES)
    monkeypatch.setattr(jurisdictions, "get_engine", lambda: engine)
    monkeypatch.setattr(db_connection, "get_engine", lambda: engine)
    monkeypatch.setattr(interface_dictation, "engine", engine)
    monkeypatch.setattr(dependencies_dictation, "get_engine", lambda: engine)
    monkeypatch.setattr(audit, "get_engine", lambda: engine)  # audit rows land here too
    content_loader.clear_cache()
    jurisdictions._jurisdiction_ids.clear()  # code -> ID, looked up once a process: not across databases
    yield engine
    content_loader.clear_cache()
    jurisdictions._jurisdiction_ids.clear()
    engine.dispose()


@pytest.fixture
def fresh_postgres():
    """A throwaway Postgres (TEST_POSTGRES_URL), emptied and migrated to head; the test
    is skipped without one. Never point TEST_POSTGRES_URL at a database you need."""
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("needs TEST_POSTGRES_URL (a throwaway Postgres)")
    from sqlalchemy import create_engine, text

    from transcribe_api.runtime import migrations

    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        migrations.run_migrations(engine)
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def jurisdiction_app():
    """DIAAT-322: the dictation and jurisdiction routes; sign in with
    jurisdiction_fixtures.client_as (one user per app)."""
    from tests.unit.jurisdiction_fixtures import jurisdiction_route_app

    return jurisdiction_route_app()


class FakeSpeechCredential:
    """Stands in for DefaultAzureCredential in stt/speech_auth.py."""

    instances = 0

    def __init__(self, *args, **kwargs) -> None:
        type(self).instances += 1
        self.scopes: list[tuple[str, ...]] = []
        self.error: Exception | None = None

    def get_token(self, *scopes, **kwargs):
        from azure.core.credentials import AccessToken

        self.scopes.append(scopes)
        if self.error is not None:
            raise self.error
        return AccessToken("aad-token", 9999999999)


@pytest.fixture
def speech_workload_identity(monkeypatch: pytest.MonkeyPatch):
    """No Speech key, as in AKS: Speech calls authenticate with a fake Entra credential.

    Returns the credential the helper uses; set ``.error`` to make get_token fail.
    """
    from transcribe_api.stt import speech_auth

    monkeypatch.setenv("AZURE_SPEECH_KEY", "")
    _clear_settings_caches()
    FakeSpeechCredential.instances = 0
    monkeypatch.setattr(speech_auth, "DefaultAzureCredential", FakeSpeechCredential)
    speech_auth._credential.cache_clear()
    credential = speech_auth._credential()
    yield credential
    speech_auth._credential.cache_clear()


def pytest_sessionstart(session):
    """DIAAT-287: no evidence from an earlier run is ever archived as this run's."""
    from tests.unit import telemetry_scan

    telemetry_scan.evidence_path(session.config.rootpath).unlink(missing_ok=True)


def pytest_sessionfinish(session, exitstatus):
    """DIAAT-287: write the session's prohibited-content scan evidence (both areas), if
    any validation scenario ran, under the repository root (TELEMETRY_PII_SCAN_REPORT
    overrides the path). Its result also fails if the test session failed."""
    import os
    import platform
    import subprocess
    from datetime import UTC, datetime
    from importlib.metadata import version

    from tests.unit import telemetry_scan

    if not telemetry_scan.SCENARIOS:
        return
    try:
        commit = (
            os.environ.get("GIT_COMMIT")
            or subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()
        )
    except Exception:
        commit = None
    telemetry_scan.write_evidence(
        telemetry_scan.evidence_path(session.config.rootpath),
        exit_status=int(exitstatus),
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        commit=commit,
        python=platform.python_version(),
        hmcts_otel=version("hmcts-otel"),
    )
