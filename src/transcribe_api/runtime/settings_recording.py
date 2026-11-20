from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# DIAAT-408: Azure Speech batch transcription with diarization fails a file longer than
# 240 minutes. Not a setting: it is Azure's limit, and PART_TARGET_SECONDS may not exceed it.
AZURE_BATCH_FILE_LIMIT_SECONDS = 14_400


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

    # DIAAT-408: the longest recording the service can accept, as decoded: 8 hours. What a
    # request is held to is the *effective* limit (stt/recording_limits.py): this value only
    # when both system feature flags `async_upload_preparation` and `long_recordings_enabled`
    # are on, otherwise at most AZURE_BATCH_FILE_LIMIT_SECONDS (240 minutes, Azure Speech's
    # limit per file for batch transcription with diarization, which every job uses). Past
    # that limit a recording has to be split before it is submitted: Azure would accept the
    # job and then fail it.
    MAX_AUDIO_DURATION_SECONDS: int = 8 * 60 * 60
    # DIAAT-408: the longest part a recording over 240 minutes is split into (3 h 30 m, a
    # margin under Azure's per-file limit). Must be between 1 second and that limit.
    PART_TARGET_SECONDS: int = 12_600
    # DIAAT-408: how many parts of one recording are prepared and submitted at once.
    MAX_PARALLEL_PARTS: int = 4
    # DIAAT-408: a backstop on the size of an uploaded file, not the limit on how long a
    # recording may be. 1 GiB takes 4 hours of any usual MP3/M4A and of 16 kHz mono WAV
    # (about 460 MB); CD-quality WAV is about 635 MB an hour. transcribe-web reads the same
    # figure from its own MAX_UPLOAD_BYTES environment variable: the two must match.
    MAX_UPLOAD_BYTES: int = 1024 * 1024 * 1024

    # DIAAT-408: asynchronous upload preparation (stt/preparation_service.py). With the system
    # flag `async_upload_preparation` on, POST /uploads stores the original file and answers at
    # once; the worker transcodes it and submits the job. How often the worker looks for jobs
    # to prepare, how many it prepares at once, how many times it tries one before failing it,
    # and how long a job being prepared may go without a sign of life before another attempt
    # takes it over (the worker renews it every PREPARE_HEARTBEAT_SECONDS).
    PREPARE_POLL_INTERVAL_SECONDS: int = 5
    PREPARE_MAX_CONCURRENT: int = 2
    PREPARE_MAX_ATTEMPTS: int = 3
    PREPARE_LEASE_SECONDS: int = 300
    PREPARE_HEARTBEAT_SECONDS: int = 30
    # How long before a storage failure is retried (multiplied by the attempt number).
    PREPARE_RETRY_BACKOFF_SECONDS: int = 30
    # Parallel block uploads of a large file to Blob Storage (the SDK default is 1, about 30 MB/s
    # measured on AAT, which would put a 1 GiB upload past the gateway's 30 s). Each in-flight
    # block is held in memory: 8 x 4 MiB. Used by the asynchronous upload path only.
    BLOB_UPLOAD_CONCURRENCY: int = 8

    # Polling
    BATCH_POLL_INTERVAL_SECONDS: int = 30
    BATCH_TRANSCRIPTION_THRESHOLD_HOURS: float = 2.0
    # DIAAT-281: a job is reported as stuck (job.stuck event, transcription.jobs.stuck
    # gauge) once it has waited longer than this since it was created, or run longer
    # than this since Azure Speech started processing it. Observation only: nothing
    # is retried or failed automatically.
    # DIAAT-408: the running threshold is 6 hours, 1.5 times the longest recording
    # accepted (MAX_AUDIO_DURATION_SECONDS), so a job near that limit is not reported as
    # stuck while it is still being processed. Azure's real processing time for a
    # 4-hour recording has not been measured: revisit this once it has been.
    JOB_STUCK_WAITING_SECONDS: int = 30 * 60
    JOB_STUCK_RUNNING_SECONDS: int = 6 * 60 * 60

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

    # DIAAT-285: enables POST /api/security/alert-test (SystemAdministrator), which
    # emits a security.ownership_violation event with outcome "test" to prove the
    # Security alert route. No effect unless ENVIRONMENT is in
    # SECURITY_ALERT_TEST_ENVIRONMENTS (routes_dictation.py: local, dev, test), so it
    # cannot be turned on in a deployed CNP environment. Leave false except during a
    # controlled test.
    SECURITY_ALERT_TEST_TRIGGER_ENABLED: bool = False

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("ENVIRONMENT", mode="before")
    @classmethod
    def normalise_environment(cls, v):
        """One spelling of an environment, as the dictation settings read it (``LOCAL`` is ``local``)."""
        return v.strip().lower() if isinstance(v, str) else v

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

    @field_validator(
        "MAX_AUDIO_DURATION_SECONDS",
        "PART_TARGET_SECONDS",
        "MAX_PARALLEL_PARTS",
        "MAX_UPLOAD_BYTES",
        "PREPARE_POLL_INTERVAL_SECONDS",
        "PREPARE_MAX_CONCURRENT",
        "PREPARE_MAX_ATTEMPTS",
        "PREPARE_LEASE_SECONDS",
        "PREPARE_HEARTBEAT_SECONDS",
        "PREPARE_RETRY_BACKOFF_SECONDS",
        "BLOB_UPLOAD_CONCURRENCY",
        mode="before",
    )
    @classmethod
    def blank_recording_limit_is_default(cls, v: object, info: ValidationInfo) -> object:
        # An env var set but empty (e.g. by a deployment template) means "use the default".
        if isinstance(v, str) and not v.strip():
            return cls.model_fields[info.field_name].default
        return v

    @field_validator(
        "MAX_AUDIO_DURATION_SECONDS",
        "MAX_PARALLEL_PARTS",
        "MAX_UPLOAD_BYTES",
        "PREPARE_POLL_INTERVAL_SECONDS",
        "PREPARE_MAX_CONCURRENT",
        "PREPARE_MAX_ATTEMPTS",
        "PREPARE_LEASE_SECONDS",
        "PREPARE_HEARTBEAT_SECONDS",
        "PREPARE_RETRY_BACKOFF_SECONDS",
        "BLOB_UPLOAD_CONCURRENCY",
    )
    @classmethod
    def validate_recording_limit(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be greater than 0")
        return v

    @field_validator("PART_TARGET_SECONDS")
    @classmethod
    def validate_part_target(cls, v: int) -> int:
        # Azure's limit is per file: a part over it would be accepted and then failed.
        if not 1 <= v <= AZURE_BATCH_FILE_LIMIT_SECONDS:
            raise ValueError(f"PART_TARGET_SECONDS must be between 1 and {AZURE_BATCH_FILE_LIMIT_SECONDS} seconds")
        return v

    @model_validator(mode="after")
    def lease_outlasts_heartbeat(self) -> Settings:
        # A job being prepared is renewed every heartbeat; a lease that could expire between two
        # renewals would let a second worker prepare (and submit) the same job.
        if self.PREPARE_LEASE_SECONDS < 2 * self.PREPARE_HEARTBEAT_SECONDS:
            raise ValueError("PREPARE_LEASE_SECONDS must be at least twice PREPARE_HEARTBEAT_SECONDS")
        return self

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
