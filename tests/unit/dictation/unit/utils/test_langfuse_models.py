"""Unit tests for Langfuse request models.

These tests validate the Langfuse Pydantic models for proper field validation,
type checking, and serialization behavior for telemetry data submission.
"""

import pytest
from pydantic import ValidationError

from transcribe_api.documents.llm.langfuse_models import (
    LangfuseScoreRequest,
    LangfuseTraceRequest,
)


class TestLangfuseTraceRequest:
    """Unit tests for the LangfuseTraceRequest model."""

    def test_trace_request_minimal_valid(self):
        """Test creating a valid trace request with only required fields."""
        trace = LangfuseTraceRequest(trace_id="trace-123", name="test-trace")

        assert trace.trace_id == "trace-123", f"Expected trace_id 'trace-123', got '{trace.trace_id}'"
        assert trace.name == "test-trace", f"Expected name 'test-trace', got '{trace.name}'"
        assert trace.metadata is None, f"Expected metadata to be None, got {trace.metadata}"
        assert trace.input_data is None, f"Expected input_data to be None, got {trace.input_data}"
        assert trace.output_data is None, f"Expected output_data to be None, got {trace.output_data}"

    def test_trace_request_with_all_fields(self):
        """Test creating a trace request with all fields populated."""
        metadata = {"user": "test-user", "session": "session-123"}
        input_data = {"query": "What is the weather?"}
        output_data = {"response": "The weather is sunny."}

        trace = LangfuseTraceRequest(
            trace_id="trace-456",
            name="weather-query",
            metadata=metadata,
            input_data=input_data,
            output_data=output_data,
        )

        assert trace.trace_id == "trace-456", f"Expected trace_id 'trace-456', got '{trace.trace_id}'"
        assert trace.name == "weather-query", f"Expected name 'weather-query', got '{trace.name}'"
        assert trace.metadata == metadata, f"Expected metadata {metadata}, got {trace.metadata}"
        assert trace.input_data == input_data, f"Expected input_data {input_data}, got {trace.input_data}"
        assert trace.output_data == output_data, f"Expected output_data {output_data}, got {trace.output_data}"

    def test_trace_request_missing_required_fields(self):
        """Test validation errors for missing required fields."""
        # Missing trace_id
        with pytest.raises(ValidationError) as exc_info:
            LangfuseTraceRequest(name="test-trace")

        error = exc_info.value
        assert "trace_id" in str(error), f"Expected 'trace_id' in validation error for missing trace_id, got: {error!s}"
        assert "Field required" in str(error), (
            f"Expected 'Field required' in validation error for missing trace_id, got: {error!s}"
        )

        # Missing name
        with pytest.raises(ValidationError) as exc_info:
            LangfuseTraceRequest(trace_id="trace-123")

        error = exc_info.value
        assert "name" in str(error), f"Expected 'name' in validation error for missing name, got: {error!s}"
        assert "Field required" in str(error), (
            f"Expected 'Field required' in validation error for missing name, got: {error!s}"
        )

    def test_trace_request_invalid_field_types(self):
        """Test validation errors for invalid field types."""
        # Non-string trace_id
        with pytest.raises(ValidationError) as exc_info:
            LangfuseTraceRequest(
                trace_id=123,  # Should be string
                name="test-trace",
            )

        error = exc_info.value
        assert "trace_id" in str(error), (
            f"Expected 'trace_id' in validation error for non-string trace_id, got: {error!s}"
        )
        assert "Input should be a valid string" in str(error), (
            f"Expected 'Input should be a valid string' in validation error for non-string trace_id, got: {error!s}"
        )

        # Non-string name
        with pytest.raises(ValidationError) as exc_info:
            LangfuseTraceRequest(
                trace_id="trace-123",
                name=456,  # Should be string
            )

        error = exc_info.value
        assert "name" in str(error), f"Expected 'name' in validation error for non-string name, got: {error!s}"
        assert "Input should be a valid string" in str(error), (
            f"Expected 'Input should be a valid string' in validation error for non-string name, got: {error!s}"
        )


class TestLangfuseScoreRequest:
    """Unit tests for the LangfuseScoreRequest model."""

    def test_score_request_minimal_valid(self):
        """Test creating a valid score request with only required fields."""
        score = LangfuseScoreRequest(trace_id="trace-123", name="user-feedback", value=4.5)

        assert score.trace_id == "trace-123", f"Expected trace_id 'trace-123', got '{score.trace_id}'"
        assert score.name == "user-feedback", f"Expected name 'user-feedback', got '{score.name}'"
        assert score.value == 4.5, f"Expected value 4.5, got {score.value}"
        assert score.comment is None, f"Expected comment to be None, got {score.comment}"

    def test_score_request_with_comment(self):
        """Test creating a score request with optional comment."""
        score = LangfuseScoreRequest(
            trace_id="trace-456", name="quality-rating", value=5.0, comment="Excellent response!"
        )

        assert score.trace_id == "trace-456", f"Expected trace_id 'trace-456', got '{score.trace_id}'"
        assert score.name == "quality-rating", f"Expected name 'quality-rating', got '{score.name}'"
        assert score.value == 5.0, f"Expected value 5.0, got {score.value}"
        assert score.comment == "Excellent response!", f"Expected comment 'Excellent response!', got '{score.comment}'"

    def test_score_request_numeric_value_types(self):
        """Test different numeric types for value field."""
        # Integer value
        score_int = LangfuseScoreRequest(trace_id="trace-int", name="integer-score", value=5)
        assert score_int.value == 5.0, (
            f"Expected integer 5 to be converted to float 5.0, got {score_int.value}"
        )  # Should be converted to float

        # Float value
        score_float = LangfuseScoreRequest(trace_id="trace-float", name="float-score", value=3.14)
        assert score_float.value == 3.14, f"Expected float value 3.14, got {score_float.value}"

    def test_score_request_missing_required_fields(self):
        """Test validation errors for missing required fields."""
        # Missing trace_id
        with pytest.raises(ValidationError) as exc_info:
            LangfuseScoreRequest(name="test-score", value=5.0)

        error = exc_info.value
        assert "trace_id" in str(error), f"Expected 'trace_id' in validation error for missing trace_id, got: {error!s}"
        assert "Field required" in str(error), (
            f"Expected 'Field required' in validation error for missing trace_id, got: {error!s}"
        )

        # Missing name
        with pytest.raises(ValidationError) as exc_info:
            LangfuseScoreRequest(trace_id="trace-123", value=5.0)

        error = exc_info.value
        assert "name" in str(error), f"Expected 'name' in validation error for missing name, got: {error!s}"
        assert "Field required" in str(error), (
            f"Expected 'Field required' in validation error for missing name, got: {error!s}"
        )

        # Missing value
        with pytest.raises(ValidationError) as exc_info:
            LangfuseScoreRequest(trace_id="trace-123", name="test-score")

        error = exc_info.value
        assert "value" in str(error), f"Expected 'value' in validation error for missing value, got: {error!s}"
        assert "Field required" in str(error), (
            f"Expected 'Field required' in validation error for missing value, got: {error!s}"
        )

    def test_score_request_invalid_value_types(self):
        """Test validation errors for invalid value types."""
        # Non-numeric value
        with pytest.raises(ValidationError) as exc_info:
            LangfuseScoreRequest(
                trace_id="trace-123",
                name="test-score",
                value="not-a-number",  # Should be numeric
            )

        error = exc_info.value
        assert "value" in str(error), f"Expected 'value' in validation error for non-numeric value, got: {error!s}"
        assert "Input should be a valid number" in str(error), (
            f"Expected 'Input should be a valid number' in validation error for non-numeric value, got: {error!s}"
        )
