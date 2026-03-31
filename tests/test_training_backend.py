"""Tests for training backend abstraction."""

from __future__ import annotations

import shutil
from unittest.mock import patch

import pytest

from lean_ai_serve.config import Settings
from lean_ai_serve.training.backend import (
    LlamaFactoryBackend,
    TrainingBackend,
    create_backend,
)
from lean_ai_serve.training.schemas import TrainingSubmitRequest


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.training.output_directory = str(tmp_path / "outputs")
    s.training.backend = "llama-factory"
    return s


@pytest.fixture
def backend(settings):
    return LlamaFactoryBackend(settings)


# ---------------------------------------------------------------------------
# ABC contract
# ---------------------------------------------------------------------------


def test_backend_is_abstract():
    """Cannot instantiate the ABC directly."""
    with pytest.raises(TypeError):
        TrainingBackend()


def test_llama_factory_is_training_backend(backend):
    """LlamaFactoryBackend is a proper subclass."""
    assert isinstance(backend, TrainingBackend)
    assert backend.name == "llama-factory"


# ---------------------------------------------------------------------------
# Environment validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_environment_found(backend):
    with patch.object(shutil, "which", return_value="/usr/bin/llamafactory-cli"):
        ok, msg = await backend.validate_environment()
    assert ok is True
    assert "llamafactory-cli found" in msg


@pytest.mark.asyncio
async def test_validate_environment_missing(backend):
    with patch.object(shutil, "which", return_value=None):
        ok, msg = await backend.validate_environment()
    assert ok is False
    assert "not found" in msg


# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_config_defaults(backend, tmp_path):
    request = TrainingSubmitRequest(
        name="test-run",
        base_model="llama-3",
        dataset="my-data",
    )
    config = await backend.build_config(
        request=request,
        dataset_path=str(tmp_path / "datasets" / "data.json"),
        model_source="meta-llama/Llama-3-8B",
        output_dir=str(tmp_path / "output"),
    )

    assert config["model_name_or_path"] == "meta-llama/Llama-3-8B"
    assert config["stage"] == "sft"
    assert config["finetuning_type"] == "lora"
    assert config["lora_rank"] == 16
    assert config["lora_alpha"] == 32
    assert config["num_train_epochs"] == 3.0
    assert config["per_device_train_batch_size"] == 4
    assert config["learning_rate"] == 2e-4
    assert config["do_train"] is True
    assert config["bf16"] is True


@pytest.mark.asyncio
async def test_build_config_custom_lora_target(backend, tmp_path):
    request = TrainingSubmitRequest(
        name="custom",
        base_model="llama-3",
        dataset="data",
        lora_target="q_proj,v_proj",
        lora_rank=32,
        lora_alpha=64,
    )
    config = await backend.build_config(
        request=request,
        dataset_path=str(tmp_path / "data.json"),
        model_source="meta-llama/Llama-3-8B",
        output_dir=str(tmp_path / "output"),
    )

    assert config["lora_target"] == "q_proj,v_proj"
    assert config["lora_rank"] == 32
    assert config["lora_alpha"] == 64


@pytest.mark.asyncio
async def test_build_config_extra_args(backend, tmp_path):
    request = TrainingSubmitRequest(
        name="extra",
        base_model="llama-3",
        dataset="data",
        extra_args={"flash_attn": True, "template": "llama3"},
    )
    config = await backend.build_config(
        request=request,
        dataset_path=str(tmp_path / "data.json"),
        model_source="model",
        output_dir=str(tmp_path / "output"),
    )

    assert config["flash_attn"] is True
    assert config["template"] == "llama3"


# ---------------------------------------------------------------------------
# Total steps estimation
# ---------------------------------------------------------------------------


def test_estimate_total_steps():
    config = {
        "num_train_epochs": 3,
        "per_device_train_batch_size": 4,
        "gradient_accumulation_steps": 4,
    }
    steps = LlamaFactoryBackend._estimate_total_steps(config)
    # 1000 / (4*4) * 3 = 187
    assert steps == 187


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_create_backend_llama_factory(settings):
    b = create_backend(settings)
    assert isinstance(b, LlamaFactoryBackend)


def test_create_backend_unknown(settings):
    settings.training.backend = "nonexistent"
    with pytest.raises(ValueError, match="Unknown training backend"):
        create_backend(settings)
