from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Core
    ENVIRONMENT: str = "local"
    DATABASE_CONNECTION_STRING: str = (
        "postgresql://dev:devpass@localhost:5432/transcription_svc"  # pragma: allowlist secret
    )

    # Azure Speech
    AZURE_SPEECH_KEY: str = ""
    AZURE_SPEECH_ENDPOINT: str = ""
    AZURE_SPEECH_RESOURCE_ID: str | None = None
    # DIAAT-282, telemetry only. The Speech resource's Azure region (e.g. uksouth),
    # recorded on job events. Needed because a custom-subdomain endpoint
    # (<name>.cognitiveservices.azure.com) does not name its region; a regional
    # endpoint (<region>.api.cognitive.microsoft.com) does, and is used when unset.
    AZURE_SPEECH_REGION: str | None = None
    # DIAAT-282, telemetry only. The number of jobs Azure Speech may run at once for
    # this resource, if PlatOps has confirmed one. When set, the
    # transcription.speech.concurrency.utilisation gauge reports running jobs / limit.
    # Unset (the default): no utilisation gauge. Nothing is throttled or queued by it.
    SPEECH_CONCURRENT_JOBS_LIMIT: int | None = None

    # Azure Storage
    AZURE_STORAGE_ACCOUNT_NAME: str = ""
    AZURE_STORAGE_CONTAINER_NAME: str = ""
    # Set in local dev to avoid mounting ~/.azure into Docker.
    # Leave unset in deployed envs — DefaultAzureCredential uses Managed Identity.
    AZURE_STORAGE_CONNECTION_STRING: str | None = None

    # Local audio storage (dev only — never set in deployed envs)
    AUDIO_STORAGE_BACKEND: Literal["azure", "local"] = "azure"
    LOCAL_AUDIO_STORAGE_DIR: str = "/tmp/local-audio"  # nosec B108  # NOSONAR
    LOCAL_AUDIO_BASE_URL: str = ""

    # Batch transcription backend (dev only — never set in deployed envs).
    # "mock" skips every Azure Speech Batch REST call in stt/batch_client.py
    # and returns a fixed canned transcript instead, so the full upload ->
    # submit -> poll -> review pipeline can be exercised with no Speech
    # resource and no network access at all.
    SPEECH_BACKEND: Literal["azure", "mock"] = "azure"

    # Polling
    BATCH_POLL_INTERVAL_SECONDS: int = 30
    BATCH_TRANSCRIPTION_THRESHOLD_HOURS: float = 2.0
    # DIAAT-281: a job is reported as stuck (job.stuck event, transcription.jobs.stuck
    # gauge) once it has waited longer than this since it was created, or run longer
    # than this since Azure Speech started processing it. Observation only: nothing
    # is retried or failed automatically.
    JOB_STUCK_WAITING_SECONDS: int = 30 * 60
    JOB_STUCK_RUNNING_SECONDS: int = 3 * 60 * 60

    # DIAAT-277: how often the background checker probes the database, Blob Storage
    # and Azure Speech (Speech at most once per 300 s whatever this is), and how long
    # one probe may take before the dependency is reported down (dependency.timeout).
    DEPENDENCY_HEALTH_INTERVAL_SECONDS: int = 60
    DEPENDENCY_HEALTH_TIMEOUT_SECONDS: float = 10.0

    # Azure AD / JWT authentication
    AZURE_AD_CLIENT_ID: str = ""
    AZURE_AD_TENANT_ID: str = ""
    JWT_ENABLE_VERIFICATION: bool = True
    JWT_VERIFICATION_STRICT: bool = True
    AUTH_APPROLES: str | None = None

    # Auth
    LOCAL_API_KEY: str = "local-dev-key-change-me"
    # Fernet key (URL-safe base64, 32 bytes) used to encrypt webhook_secret at rest.
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # The default is a fixed all-zeros key for local development only.
    WEBHOOK_SECRET_ENCRYPTION_KEY: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="  # gitleaks:allow

    # Webhook delivery
    WEBHOOK_TIMEOUT_SECONDS: float = 30.0
    WEBHOOK_MAX_RETRIES: int = 3

    # Optional per-environment override for the review-highlighting cutoff
    # (see audio/accuracy.py DEFAULT_CONFIDENCE_THRESHOLD). Expressed as a
    # 0-1 ratio, matching Azure's per-word confidence scale — NOT a percent.
    LOW_CONFIDENCE_THRESHOLD: float | None = None

    # Corrections dataset export (DIAAT-231)
    # Gates whether clerk corrections (word-range and whole-segment) are
    # additionally persisted into the correction_dataset_entry table, a
    # durable store separate from per-job transcript data meant for later
    # export to fine-tune/evaluate transcription models.
    #
    # Defaults to False: this table can capture real court-hearing content,
    # and retention/anonymisation policy for it has NOT yet been defined or
    # signed off by legal/compliance (see DIAAT-231 acceptance criteria #3,
    # and the docstring on CorrectionDatasetEntry). Do not set this to True
    # in any environment handling real hearing content until that sign-off
    # has happened — until then, the write path exists and is testable, but
    # stays off so no real transcript content is silently captured.
    CORRECTIONS_DATASET_EXPORT_ENABLED: bool = False

    # Observability
    SENTRY_DSN: str | None = None
    # DIAAT-285: enables POST /api/security/alert-test (SystemAdministrator), which
    # emits a security.ownership_violation event with outcome "test" to prove the
    # Security alert route. No effect unless ENVIRONMENT is in
    # SECURITY_ALERT_TEST_ENVIRONMENTS (routes_dictation.py: local, dev, test), so it
    # cannot be turned on in a deployed CNP environment. Leave false except during a
    # controlled test.
    SECURITY_ALERT_TEST_TRIGGER_ENABLED: bool = False

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("SECURITY_ALERT_TEST_TRIGGER_ENABLED", mode="before")
    @classmethod
    def blank_security_alert_trigger_is_disabled(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return False
        return v

    @field_validator("BATCH_TRANSCRIPTION_THRESHOLD_HOURS")
    @classmethod
    def validate_threshold(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("BATCH_TRANSCRIPTION_THRESHOLD_HOURS must be greater than 0")
        return v

    @field_validator("JOB_STUCK_WAITING_SECONDS", "JOB_STUCK_RUNNING_SECONDS", mode="before")
    @classmethod
    def blank_stuck_threshold_is_default(cls, v: object, info: ValidationInfo) -> object:
        # An env var set but empty (e.g. by a deployment template) means "use the default",
        # not an int parsing error that stops the service starting.
        if isinstance(v, str) and not v.strip():
            return cls.model_fields[info.field_name].default
        return v

    @field_validator("JOB_STUCK_WAITING_SECONDS", "JOB_STUCK_RUNNING_SECONDS")
    @classmethod
    def validate_stuck_threshold(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Stuck-job thresholds must be greater than 0 seconds")
        return v

    @field_validator(
        "DEPENDENCY_HEALTH_INTERVAL_SECONDS", "DEPENDENCY_HEALTH_TIMEOUT_SECONDS", mode="before"
    )
    @classmethod
    def blank_dependency_health_setting_is_default(cls, v: object, info: ValidationInfo) -> object:
        if isinstance(v, str) and not v.strip():
            return cls.model_fields[info.field_name].default
        return v

    @field_validator("DEPENDENCY_HEALTH_INTERVAL_SECONDS", "DEPENDENCY_HEALTH_TIMEOUT_SECONDS")
    @classmethod
    def validate_dependency_health_setting(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Dependency health interval and timeout must be greater than 0")
        return v

    @field_validator("SPEECH_CONCURRENT_JOBS_LIMIT", mode="before")
    @classmethod
    def blank_concurrent_jobs_limit_is_unset(cls, v: object) -> object:
        # An env var set but empty means unset, not an int parsing error at start-up.
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("SPEECH_CONCURRENT_JOBS_LIMIT")
    @classmethod
    def validate_concurrent_jobs_limit(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("SPEECH_CONCURRENT_JOBS_LIMIT must be greater than 0")
        return v

    @field_validator("LOW_CONFIDENCE_THRESHOLD")
    @classmethod
    def validate_low_confidence_threshold(cls, v: float | None) -> float | None:
        # Guard against the common percent-vs-ratio misconfiguration (e.g.
        # setting 65 instead of 0.65, which would flag every word). Azure's
        # per-word confidence is a 0-1 ratio; 0.0 (flag nothing) is allowed.
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError("LOW_CONFIDENCE_THRESHOLD must be a ratio between 0 and 1")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
