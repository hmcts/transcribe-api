"""Pytest configuration and shared fixtures."""

import asyncio
import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import AsyncClient

# Add the backend directory to Python path for imports
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))


def create_test_csv_data(rows: list[tuple[str, str]]) -> str:
    """Create properly formatted CSV test data with realistic formatting variations.

    Parameters
    ----------
    rows : list[tuple[str, str]]
        List of (provider, email) tuples with potential formatting variations.

    Returns
    -------
    str
        Properly formatted CSV string with realistic whitespace and case variations.
    """
    csv_lines = ["Provider,Email"]
    for provider, email in rows:
        # Add realistic formatting variations that would be cleaned by parsing
        formatted_provider = f"  {provider}  " if len(provider) > 5 else provider
        formatted_email = f"  {email}  " if "@" in email else email
        csv_lines.append(f"{formatted_provider},{formatted_email}")

    return "\n".join(csv_lines) + "\n"


def clean_email_for_comparison(email: str) -> str:
    """Clean email for robust test comparisons.

    Parameters
    ----------
    email : str
        Email to clean.

    Returns
    -------
    str
        Cleaned email (lowercased and stripped).
    """
    return email.strip().lower()


def pytest_addoption(parser):
    """Add command line options for test configuration."""
    parser.addoption(
        "--integration", action="store_true", default=False, help="Run integration tests (skipped by default)"
    )
    parser.addoption(
        "--allow-network",
        action="store_true",
        default=False,
        help="Allow tests that require network access (disabled by default)",
    )
    parser.addoption(
        "--data-validation", action="store_true", default=False, help="Run data validation tests (skipped by default)"
    )


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_settings() -> MagicMock:
    """Mock application settings."""
    settings = MagicMock()
    settings.DATABASE_URL = "postgresql://test:test@localhost:5432/test_db"
    settings.AZURE_CONTAINER_NAME = "test-container"
    settings.OPENAI_API_KEY = "test-openai-key"
    settings.ENVIRONMENT = "test"
    return settings


@pytest.fixture
def mock_database():
    """Mock database session."""
    return MagicMock()


@pytest.fixture
def mock_azure_client() -> MagicMock:
    """Mock Azure Blob Service Client."""
    mock_client = MagicMock()
    mock_blob_client = MagicMock()
    mock_client.get_blob_client.return_value = mock_blob_client
    mock_blob_client.upload_blob.return_value = None
    mock_blob_client.download_blob.return_value.readall.return_value = b"test audio data"
    return mock_client


@pytest.fixture
def mock_openai_client() -> AsyncMock:
    """Mock OpenAI client for testing LLM interactions."""
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Test AI response"
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
    return mock_client


@pytest.fixture
def sample_audio_file(tmp_path: Path) -> Path:
    """Create a sample audio file for testing."""
    audio_file = tmp_path / "test_audio.wav"
    # Create a minimal WAV file header
    wav_header = (
        b"RIFF"
        + (44 - 8).to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")  # PCM format
        + (1).to_bytes(2, "little")  # mono
        + (44100).to_bytes(4, "little")  # sample rate
        + (88200).to_bytes(4, "little")  # byte rate
        + (2).to_bytes(2, "little")  # block align
        + (16).to_bytes(2, "little")  # bits per sample
        + b"data"
        + (0).to_bytes(4, "little")  # data size
    )
    audio_file.write_bytes(wav_header)
    return audio_file


@pytest.fixture
def sample_transcript() -> dict[str, Any]:
    """Sample transcript data for testing."""
    return {
        "text": "This is a test transcript with multiple speakers discussing important topics.",
        "speakers": [
            {
                "id": "speaker_1",
                "name": "John Doe",
                "segments": [
                    {"start": 0.0, "end": 5.0, "text": "This is a test transcript"},
                ],
            },
            {
                "id": "speaker_2",
                "name": "Jane Smith",
                "segments": [
                    {"start": 5.0, "end": 10.0, "text": "with multiple speakers discussing"},
                ],
            },
        ],
        "duration": 10.0,
    }


@pytest.fixture
def sample_meeting_data() -> dict[str, Any]:
    """Sample meeting data for testing."""
    return {
        "title": "Test Meeting",
        "agenda": "Discuss test procedures and implementation",
        "participants": ["John Doe", "Jane Smith", "Bob Johnson"],
        "date": "2024-01-15",
        "duration": 3600,  # 1 hour in seconds
    }


@pytest_asyncio.fixture
async def test_client() -> AsyncGenerator[AsyncClient, None]:
    """Create test client for API testing."""
    # Import here to avoid circular imports
    from main import app

    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client


@pytest.fixture
def sync_test_client() -> TestClient:
    """Create synchronous test client for API testing."""
    from main import app

    return TestClient(app)


# Environment setup for tests
@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set up test environment variables."""
    test_env_vars = {
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
        "AZURE_STORAGE_ACCOUNT_NAME": "teststorage",
        "AZURE_STORAGE_CONTAINER_NAME": "test-container",
        "AZURE_STORAGE_TRANSCRIPTION_CONTAINER": "test-transcription-container",
        "DATABASE_CONNECTION_STRING": "postgresql://test:test@localhost:5432/test_db",
        "GOOGLE_APPLICATION_CREDENTIALS_JSON_OBJECT": "{}",
        "GOV_NOTIFY_API_KEY": "test-notify-key",
        "LANGFUSE_PUBLIC_KEY": "pk-lf-test-public-key-123456",
        "LANGFUSE_SECRET_KEY": "sk-lf-test-secret-key-123456",
        "LANGFUSE_HOST": "https://langfuse-ai.justice.gov.uk",
        "SENTRY_DSN": "",  # Disable Sentry in tests
        "JWT_SECRET_KEY": "test-jwt-secret",
    }

    for key, value in test_env_vars.items():
        monkeypatch.setenv(key, value)


# Pytest configuration
def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest with custom markers and socket settings."""
    # Silence the langfuse logger — the test suite uses fake credentials which
    # cause the Langfuse SDK's atexit flush to fail and log error messages.
    # We use a Filter (not setLevel) because the SDK resets the level to WARNING
    # in Langfuse.__init__, which would re-enable the error messages.
    import logging

    class _SilenceLangfuse(logging.Filter):
        def filter(self, record):
            return False

    logging.getLogger("langfuse").addFilter(_SilenceLangfuse())

    config.addinivalue_line("markers", "unit: mark test as a unit test")
    config.addinivalue_line("markers", "integration: mark test as an integration test")
    config.addinivalue_line("markers", "e2e: mark test as an end-to-end test")
    config.addinivalue_line("markers", "slow: mark test as slow running")
    config.addinivalue_line("markers", "azure: mark test as requiring Azure services")
    config.addinivalue_line("markers", "database: mark test as requiring database")
    config.addinivalue_line("markers", "network: mark test as requiring network access")
    config.addinivalue_line("markers", "external_api: mark test as calling external APIs")

    # Configure socket blocking based on flags
    allow_network = config.getoption("--allow-network")
    run_integration = config.getoption("--integration")

    # Only enable socket blocking for pure unit tests
    # Integration tests and network tests need sockets for async loops and network calls
    if not allow_network and not run_integration:
        # Only block network sockets, allow unix sockets for async loops
        import pytest_socket

        pytest_socket.disable_socket(allow_unix_socket=True)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Automatically mark tests based on their location and skip integration tests by default."""
    # Skip integration tests by default unless --integration flag is used
    skip_integration = not config.getoption("--integration")
    # Skip network tests by default unless --allow-network flag is used
    allow_network = config.getoption("--allow-network")

    for item in items:
        # MERGE NOTE: this conftest now sits alongside a sibling suite
        # (tests/recording/), so collection includes items outside its own tree.
        # Only auto-mark items belonging to this suite; leave the sibling's to
        # its own conftest.
        item_path = Path(item.fspath)
        suite_root = Path(__file__).parent
        if not item_path.is_relative_to(suite_root):
            continue
        test_path = item_path.relative_to(suite_root)

        # Auto-mark based on directory structure
        if test_path.parts[0] == "unit":
            item.add_marker(pytest.mark.unit)
        elif test_path.parts[0] == "integration":
            item.add_marker(pytest.mark.integration)
            # Skip integration tests by default
            if skip_integration:
                item.add_marker(pytest.mark.skip(reason="Integration test skipped. Use --integration to run."))
        elif test_path.parts[0] == "e2e":
            item.add_marker(pytest.mark.e2e)

        # Skip network tests if they're marked and network is not allowed
        if not allow_network:
            for marker in item.iter_markers():
                if marker.name in ("network", "external_api"):
                    item.add_marker(pytest.mark.skip(reason="Network test skipped. Use --allow-network to run."))
