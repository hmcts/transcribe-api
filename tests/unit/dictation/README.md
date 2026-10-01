# JusticeTranscribe Backend Tests

This directory contains the complete test suite for the JusticeTranscribe backend application using pytest.

## Directory Structure

```
tests/
├── __init__.py
├── conftest.py              # Pytest configuration and shared fixtures
├── README.md               # This file
├── unit/                   # Unit tests - test individual functions/classes
│   ├── __init__.py
│   ├── app/               # Tests for app package
│   │   ├── audio/         # Audio processing tests
│   │   ├── database/      # Database model tests
│   │   ├── llm/          # LLM integration tests
│   │   └── minutes/      # Minutes generation tests
│   ├── api/              # API route unit tests
│   └── utils/            # Utility function tests
├── integration/           # Integration tests - test multiple components
│   ├── __init__.py
│   └── test_api_routes.py
└── e2e/                  # End-to-end tests - test complete workflows
    ├── __init__.py
    └── test_user_workflow.py
```

## Test Types

### Unit Tests (`tests/unit/`)
- Test individual functions, classes, and methods in isolation
- Fast execution (< 1 second each)
- Mock external dependencies
- High code coverage focus

### Integration Tests (`tests/integration/`)
- Test interaction between multiple components
- Test API endpoints with real request/response cycles
- May use test databases or mock services
- Medium execution time (1-10 seconds each)

### End-to-End Tests (`tests/e2e/`)
- Test complete user workflows
- Test the application as a whole
- May use external services (with mocking)
- Slower execution (10+ seconds each)

## Running Tests

### Prerequisites

Install development dependencies:
```bash
# Using uv (recommended)
uv sync --group dev

# Or using pip
pip install -e ".[dev]"
```

### Basic Test Commands

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run tests in parallel (faster)
pytest -n auto

# Run only unit tests
pytest tests/unit/

# Run only integration tests
pytest tests/integration/

# Run only end-to-end tests
pytest tests/e2e/

# Run tests by marker
pytest -m unit
pytest -m integration
pytest -m e2e
pytest -m "not slow"
```

### Coverage Reports

```bash
# Run tests with coverage
pytest --cov

# Generate HTML coverage report
pytest --cov --cov-report=html

# View coverage report
open htmlcov/index.html
```

### Test Markers

Tests are organized using pytest markers:

- `@pytest.mark.unit` - Unit tests
- `@pytest.mark.integration` - Integration tests
- `@pytest.mark.e2e` - End-to-end tests
- `@pytest.mark.slow` - Tests that take longer to run
- `@pytest.mark.azure` - Tests requiring Azure services
- `@pytest.mark.database` - Tests requiring database access
- `@pytest.mark.auth` - Authentication-related tests
- `@pytest.mark.api` - API endpoint tests
- `@pytest.mark.audio` - Audio processing tests
- `@pytest.mark.llm` - LLM/AI functionality tests

### Selective Test Execution

```bash
# Run only fast tests
pytest -m "not slow"

# Run only Azure-related tests
pytest -m azure

# Run audio tests but skip slow ones
pytest -m "audio and not slow"

# Run specific test file
pytest tests/unit/app/audio/test_azure_utils.py

# Run specific test function
pytest tests/unit/utils/test_markdown.py::test_markdown_to_html_basic
```

## Writing Tests

### Test Naming Convention

- Test files: `test_*.py`
- Test functions: `test_*`
- Test classes: `Test*`

### Using Fixtures

Common fixtures are available in `conftest.py`:

```python
def test_my_function(mock_settings, mock_database):
    # Use fixtures in your tests
    pass

async def test_async_function(test_client, sample_audio_file):
    # Test async functions
    pass
```

### Adding New Tests

1. **Unit Tests**: Place in `tests/unit/` following the app structure
2. **Integration Tests**: Place in `tests/integration/`
3. **E2E Tests**: Place in `tests/e2e/`

Example unit test:
```python
import pytest
from app.my_module import my_function

@pytest.mark.unit
def test_my_function():
    result = my_function("input")
    assert result == "expected_output"
```

Example integration test:
```python
import pytest

@pytest.mark.integration
@pytest.mark.api
async def test_api_endpoint(test_client):
    response = await test_client.get("/api/endpoint")
    assert response.status_code == 200
```

## Configuration

### pytest.ini
- Test discovery patterns
- Coverage settings
- Marker definitions
- Warning filters

### conftest.py
- Shared fixtures
- Test environment setup
- Mock configurations
- Automatic test marking

## Continuous Integration

Tests run automatically on:
- Pull requests
- Main branch commits
- Nightly builds

CI configuration includes:
- Multiple Python versions
- Different environments
- Coverage reporting
- Performance benchmarks

## Best Practices

1. **Keep tests isolated** - Each test should be independent
2. **Use descriptive names** - Test names should explain what they test
3. **Mock external dependencies** - Don't rely on external services
4. **Test edge cases** - Include boundary conditions and error cases
5. **Keep tests fast** - Unit tests should run in milliseconds
6. **Use appropriate markers** - Mark tests for selective execution
7. **Maintain test data** - Use fixtures for consistent test data

## Debugging Tests

```bash
# Run tests with Python debugger
pytest --pdb

# Run tests with more verbose output
pytest -vv

# Show local variables in tracebacks
pytest -l

# Stop on first failure
pytest -x

# Show slowest tests
pytest --durations=10
```

## Troubleshooting

### Common Issues

1. **Import Errors**: Ensure the backend directory is in PYTHONPATH
2. **Async Test Issues**: Use `pytest-asyncio` and `@pytest.mark.asyncio`
3. **Database Issues**: Use test database or mocks
4. **External Service Issues**: Mock all external dependencies
5. **Coverage Issues**: Ensure all code paths are tested

### Getting Help

- Check pytest documentation: https://docs.pytest.org/
- Review existing tests for patterns
- Use `pytest --help` for command options
- Check CI logs for detailed error information

<!-- ci: trigger image rebuild 2026-04-30 -->

