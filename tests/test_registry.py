"""Tests for the model registry."""

from __future__ import annotations

import pytest

from lean_ai_serve.config import ModelConfig
from lean_ai_serve.db import Database
from lean_ai_serve.models.registry import ModelRegistry
from lean_ai_serve.models.schemas import ModelState


@pytest.fixture
async def registry(tmp_path) -> ModelRegistry:
    db = Database(tmp_path / "test.db")
    await db.connect()
    r = ModelRegistry(db)
    yield r
    await db.close()


async def test_sync_from_config(registry: ModelRegistry):
    """sync_from_config should register new models."""
    models = {
        "test-model": ModelConfig(source="org/test-model"),
        "other-model": ModelConfig(source="org/other", gpu=[0, 1]),
    }
    await registry.sync_from_config(models)

    result = await registry.list_models()
    assert len(result) == 2
    names = {m.name for m in result}
    assert "test-model" in names
    assert "other-model" in names


async def test_sync_preserves_state(registry: ModelRegistry):
    """Re-syncing should not reset model state."""
    models = {"test": ModelConfig(source="org/test")}
    await registry.sync_from_config(models)

    # Change state
    await registry.set_state("test", ModelState.DOWNLOADED)

    # Re-sync
    await registry.sync_from_config(models)

    model = await registry.get_model("test")
    assert model.state == ModelState.DOWNLOADED


async def test_set_state(registry: ModelRegistry):
    """set_state should update model state and optional fields."""
    await registry.register_model("test", "org/test", ModelConfig(source="org/test"))

    await registry.set_state("test", ModelState.LOADED, port=8430, pid=1234)
    model = await registry.get_model("test")
    assert model.state == ModelState.LOADED
    assert model.port == 8430


async def test_set_state_error(registry: ModelRegistry):
    """Setting ERROR state should store error message."""
    await registry.register_model("test", "org/test", ModelConfig(source="org/test"))

    await registry.set_state("test", ModelState.ERROR, error_message="CUDA OOM")
    model = await registry.get_model("test")
    assert model.state == ModelState.ERROR
    assert model.error_message == "CUDA OOM"


async def test_delete_model(registry: ModelRegistry):
    """delete_model should remove the model."""
    await registry.register_model("test", "org/test", ModelConfig(source="org/test"))
    assert await registry.delete_model("test") is True
    assert await registry.get_model("test") is None


async def test_delete_nonexistent(registry: ModelRegistry):
    """delete_model should return False for missing models."""
    assert await registry.delete_model("nope") is False


async def test_get_config(registry: ModelRegistry):
    """get_config should return the stored ModelConfig."""
    config = ModelConfig(source="org/test", tensor_parallel_size=2, enable_lora=True)
    await registry.register_model("test", "org/test", config)

    loaded = await registry.get_config("test")
    assert loaded is not None
    assert loaded.source == "org/test"
    assert loaded.tensor_parallel_size == 2
    assert loaded.enable_lora is True
