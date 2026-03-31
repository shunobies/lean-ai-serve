"""Tests for model configuration validators."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lean_ai_serve.config import ModelConfig, SpeculativeConfig
from lean_ai_serve.engine.validators import (
    validate_gpu_config,
    validate_gpu_existence,
    validate_model_config,
    validate_speculative_config,
)

# ---------------------------------------------------------------------------
# GPU config validation
# ---------------------------------------------------------------------------


def test_valid_gpu_config():
    config = ModelConfig(source="org/model", gpu=[0, 1], tensor_parallel_size=2)
    assert validate_gpu_config(config) == []


def test_valid_single_gpu():
    config = ModelConfig(source="org/model", gpu=[0], tensor_parallel_size=1)
    assert validate_gpu_config(config) == []


def test_tp_exceeds_gpu_count():
    config = ModelConfig(source="org/model", gpu=[0], tensor_parallel_size=4)
    errors = validate_gpu_config(config)
    assert len(errors) >= 1
    assert any("tensor_parallel_size (4)" in e for e in errors)


def test_pp_times_tp_exceeds_gpus():
    config = ModelConfig(
        source="org/model",
        gpu=[0, 1],
        tensor_parallel_size=2,
        pipeline_parallel_size=2,
    )
    errors = validate_gpu_config(config)
    assert len(errors) == 1
    assert "tensor_parallel_size * pipeline_parallel_size (4)" in errors[0]


def test_tp_and_pp_within_limits():
    config = ModelConfig(
        source="org/model",
        gpu=[0, 1, 2, 3],
        tensor_parallel_size=2,
        pipeline_parallel_size=2,
    )
    assert validate_gpu_config(config) == []


# ---------------------------------------------------------------------------
# GPU existence validation
# ---------------------------------------------------------------------------


def test_gpu_existence_all_present():
    mock_pynvml = MagicMock()
    mock_pynvml.nvmlDeviceGetCount.return_value = 4
    with patch.dict("sys.modules", {"pynvml": mock_pynvml}):
        config = ModelConfig(source="org/model", gpu=[0, 1, 2])
        errors = validate_gpu_existence(config)
    assert errors == []


def test_gpu_existence_missing():
    mock_pynvml = MagicMock()
    mock_pynvml.nvmlDeviceGetCount.return_value = 2
    with patch.dict("sys.modules", {"pynvml": mock_pynvml}):
        config = ModelConfig(source="org/model", gpu=[0, 5])
        errors = validate_gpu_existence(config)
    assert len(errors) == 1
    assert "GPU index 5" in errors[0]
    assert "2 GPU(s)" in errors[0]


def test_gpu_existence_no_pynvml():
    """Graceful fallback when pynvml not available."""
    with patch.dict("sys.modules", {"pynvml": None}):
        config = ModelConfig(source="org/model", gpu=[0, 99])
        # Should not raise, just skip the check
        errors = validate_gpu_existence(config)
        assert errors == []


# ---------------------------------------------------------------------------
# Speculative config validation
# ---------------------------------------------------------------------------


def test_speculative_disabled():
    config = ModelConfig(source="org/model")
    assert validate_speculative_config(config) == []


def test_speculative_draft_valid():
    config = ModelConfig(
        source="org/model",
        speculative=SpeculativeConfig(
            enabled=True, strategy="draft", draft_model="org/draft", num_tokens=5
        ),
    )
    assert validate_speculative_config(config) == []


def test_speculative_draft_no_model():
    config = ModelConfig(
        source="org/model",
        speculative=SpeculativeConfig(enabled=True, strategy="draft"),
    )
    errors = validate_speculative_config(config)
    assert any("draft_model" in e for e in errors)


def test_speculative_eagle_warning():
    config = ModelConfig(
        source="org/model",
        speculative=SpeculativeConfig(enabled=True, strategy="eagle"),
    )
    errors = validate_speculative_config(config)
    assert any("eagle" in e.lower() for e in errors)


def test_speculative_ngram_valid():
    config = ModelConfig(
        source="org/model",
        speculative=SpeculativeConfig(
            enabled=True, strategy="ngram", num_tokens=10
        ),
    )
    assert validate_speculative_config(config) == []


def test_speculative_num_tokens_out_of_range():
    config = ModelConfig(
        source="org/model",
        speculative=SpeculativeConfig(
            enabled=True, strategy="ngram", num_tokens=50
        ),
    )
    errors = validate_speculative_config(config)
    assert any("num_tokens" in e for e in errors)


# ---------------------------------------------------------------------------
# Combined validator
# ---------------------------------------------------------------------------


def test_validate_model_config_valid():
    config = ModelConfig(source="org/model", gpu=[0], tensor_parallel_size=1)
    # Should not raise
    validate_model_config(config)


def test_validate_model_config_raises_on_error():
    config = ModelConfig(source="org/model", gpu=[0], tensor_parallel_size=4)
    with pytest.raises(ValueError, match="validation failed"):
        validate_model_config(config)


def test_validate_model_config_combines_errors():
    config = ModelConfig(
        source="org/model",
        gpu=[0],
        tensor_parallel_size=4,
        speculative=SpeculativeConfig(
            enabled=True, strategy="draft", num_tokens=50
        ),
    )
    with pytest.raises(ValueError) as exc_info:
        validate_model_config(config)
    msg = str(exc_info.value)
    assert "tensor_parallel_size" in msg
    assert "draft_model" in msg
    assert "num_tokens" in msg
