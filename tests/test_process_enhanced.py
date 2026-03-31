"""Tests for process.py enhancements — CUDA scoping, validation, KV cache."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import pytest

from lean_ai_serve.config import KVCacheConfig, ModelConfig, Settings, set_settings
from lean_ai_serve.engine.process import ProcessManager


def _make_settings(models: dict[str, ModelConfig] | None = None) -> Settings:
    s = Settings(models=models or {})
    set_settings(s)
    return s


# ---------------------------------------------------------------------------
# CUDA_VISIBLE_DEVICES scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cuda_visible_devices_single_gpu():
    """Single GPU config scopes CUDA_VISIBLE_DEVICES to that GPU."""
    config = ModelConfig(source="org/model", gpu=[2])
    _make_settings({"test-model": config})

    pm = ProcessManager()
    captured_env = {}

    async def fake_exec(*args, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        proc = AsyncMock()
        proc.pid = 12345
        proc.returncode = None
        proc.stderr = AsyncMock()
        proc.stdout = AsyncMock()
        return proc

    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("lean_ai_serve.engine.validators.validate_model_config"),
        patch("lean_ai_serve.utils.gpu.get_free_port", return_value=8430),
    ):
        info = await pm.start("test-model", config, "/models/org/model")

    assert captured_env["CUDA_VISIBLE_DEVICES"] == "2"
    # Cleanup
    if info._health_task:
        info._health_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await info._health_task
    await pm._http.aclose()


@pytest.mark.asyncio
async def test_cuda_visible_devices_multi_gpu():
    """Multi-GPU config scopes CUDA_VISIBLE_DEVICES to those GPUs."""
    config = ModelConfig(source="org/model", gpu=[0, 2, 5])
    _make_settings({"test-model": config})

    pm = ProcessManager()
    captured_env = {}

    async def fake_exec(*args, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        proc = AsyncMock()
        proc.pid = 12345
        proc.returncode = None
        proc.stderr = AsyncMock()
        proc.stdout = AsyncMock()
        return proc

    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("lean_ai_serve.engine.validators.validate_model_config"),
        patch("lean_ai_serve.utils.gpu.get_free_port", return_value=8430),
    ):
        info = await pm.start("test-model", config, "/models/org/model")

    assert captured_env["CUDA_VISIBLE_DEVICES"] == "0,2,5"
    if info._health_task:
        info._health_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await info._health_task
    await pm._http.aclose()


# ---------------------------------------------------------------------------
# Validation called before start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_called_before_start():
    """validate_model_config is called before spawning the process."""
    config = ModelConfig(source="org/model", gpu=[0])
    _make_settings({"test-model": config})

    pm = ProcessManager()

    with (
        patch(
            "lean_ai_serve.engine.validators.validate_model_config",
            side_effect=ValueError("validation failed: bad config"),
        ) as mock_validate,
        patch("lean_ai_serve.utils.gpu.get_free_port", return_value=8430),
        pytest.raises(ValueError, match="validation failed"),
    ):
        await pm.start("test-model", config, "/models/org/model")

    mock_validate.assert_called_once_with(config)
    await pm._http.aclose()


@pytest.mark.asyncio
async def test_validation_passes_then_spawns():
    """When validation passes, process is spawned."""
    config = ModelConfig(source="org/model", gpu=[0])
    _make_settings({"test-model": config})

    pm = ProcessManager()
    spawn_called = False

    async def fake_exec(*args, **kwargs):
        nonlocal spawn_called
        spawn_called = True
        proc = AsyncMock()
        proc.pid = 12345
        proc.returncode = None
        proc.stderr = AsyncMock()
        proc.stdout = AsyncMock()
        return proc

    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("lean_ai_serve.engine.validators.validate_model_config"),
        patch("lean_ai_serve.utils.gpu.get_free_port", return_value=8430),
    ):
        info = await pm.start("test-model", config, "/models/org/model")

    assert spawn_called
    assert info.pid == 12345
    if info._health_task:
        info._health_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await info._health_task
    await pm._http.aclose()


# ---------------------------------------------------------------------------
# KV cache calculate_scales flag
# ---------------------------------------------------------------------------


def test_calculate_kv_scales_in_command():
    """--calculate-kv-scales appears when calculate_scales is True."""
    config = ModelConfig(
        source="org/model",
        kv_cache=KVCacheConfig(dtype="fp8", calculate_scales=True),
    )
    _make_settings({"test-model": config})

    pm = ProcessManager()
    cmd = pm._build_command("test-model", config, "/models/org/model", 8430)

    assert "--kv-cache-dtype" in cmd
    assert "fp8" in cmd
    assert "--calculate-kv-scales" in cmd


def test_no_calculate_kv_scales_when_false():
    """--calculate-kv-scales absent when calculate_scales is False (default)."""
    config = ModelConfig(
        source="org/model",
        kv_cache=KVCacheConfig(dtype="fp8"),
    )
    _make_settings({"test-model": config})

    pm = ProcessManager()
    cmd = pm._build_command("test-model", config, "/models/org/model", 8430)

    assert "--kv-cache-dtype" in cmd
    assert "--calculate-kv-scales" not in cmd


def test_kv_cache_auto_dtype_no_flags():
    """When kv_cache dtype is 'auto', no KV cache flags should appear."""
    config = ModelConfig(source="org/model")
    _make_settings({"test-model": config})

    pm = ProcessManager()
    cmd = pm._build_command("test-model", config, "/models/org/model", 8430)

    assert "--kv-cache-dtype" not in cmd
    assert "--calculate-kv-scales" not in cmd


# ---------------------------------------------------------------------------
# Command building — smoke test for key flags
# ---------------------------------------------------------------------------


def test_build_command_includes_basic_flags():
    """Basic command includes model, served-model-name, host, port, dtype."""
    config = ModelConfig(source="org/model")
    _make_settings({"test-model": config})

    pm = ProcessManager()
    cmd = pm._build_command("test-model", config, "/models/org/model", 8430)

    assert "--model" in cmd
    assert "/models/org/model" in cmd
    assert "--served-model-name" in cmd
    assert "test-model" in cmd
    assert "--host" in cmd
    assert "127.0.0.1" in cmd
    assert "--port" in cmd
    assert "8430" in cmd


def test_build_command_tensor_parallel():
    """Tensor parallel flag appears when tp > 1."""
    config = ModelConfig(source="org/model", gpu=[0, 1], tensor_parallel_size=2)
    _make_settings({"test-model": config})

    pm = ProcessManager()
    cmd = pm._build_command("test-model", config, "/models/org/model", 8430)

    assert "--tensor-parallel-size" in cmd
    idx = cmd.index("--tensor-parallel-size")
    assert cmd[idx + 1] == "2"


def test_build_command_no_tp_when_default():
    """No --tensor-parallel-size when tp == 1 (default)."""
    config = ModelConfig(source="org/model")
    _make_settings({"test-model": config})

    pm = ProcessManager()
    cmd = pm._build_command("test-model", config, "/models/org/model", 8430)

    assert "--tensor-parallel-size" not in cmd
