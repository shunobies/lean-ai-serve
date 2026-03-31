"""Tests for adapter registry."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from lean_ai_serve.db import Database
from lean_ai_serve.training.adapters import AdapterError, AdapterRegistry
from lean_ai_serve.training.schemas import AdapterState


@pytest_asyncio.fixture
async def db(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def registry(db):
    reg = AdapterRegistry(db)
    yield reg
    await reg.close()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_adapter(registry, tmp_path):
    # Create a fake adapter directory
    adapter_dir = tmp_path / "adapter_weights"
    adapter_dir.mkdir()

    info = await registry.register(
        name="my-adapter",
        base_model="llama-3",
        source_path=str(adapter_dir),
        training_job_id="job-123",
        metadata={"loss": 0.5},
    )

    assert info.name == "my-adapter"
    assert info.base_model == "llama-3"
    assert info.state == AdapterState.AVAILABLE
    assert info.training_job_id == "job-123"
    assert info.metadata == {"loss": 0.5}


@pytest.mark.asyncio
async def test_register_duplicate_rejected(registry, tmp_path):
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()

    await registry.register("dup", "model", str(adapter_dir))
    with pytest.raises(ValueError, match="already exists"):
        await registry.register("dup", "model", str(adapter_dir))


@pytest.mark.asyncio
async def test_register_nonexistent_path(registry):
    with pytest.raises(AdapterError, match="does not exist"):
        await registry.register("bad", "model", "/nonexistent/path")


# ---------------------------------------------------------------------------
# List and get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_adapters(registry, tmp_path):
    d1 = tmp_path / "a1"
    d1.mkdir()
    d2 = tmp_path / "a2"
    d2.mkdir()

    await registry.register("adapter-1", "llama-3", str(d1))
    await registry.register("adapter-2", "mistral", str(d2))

    all_adapters = await registry.list_adapters()
    assert len(all_adapters) == 2

    # Filter by base model
    llama_only = await registry.list_adapters(base_model="llama-3")
    assert len(llama_only) == 1
    assert llama_only[0].name == "adapter-1"


@pytest.mark.asyncio
async def test_get_adapter(registry, tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    await registry.register("test-get", "model", str(d))

    info = await registry.get("test-get")
    assert info is not None
    assert info.name == "test-get"

    assert await registry.get("nope") is None


# ---------------------------------------------------------------------------
# Deploy / undeploy (mocked HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_adapter(registry, tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    await registry.register("deploy-me", "model", str(d))

    # Mock the vLLM HTTP call
    mock_response = AsyncMock()
    mock_response.status_code = 200

    with patch.object(registry._http, "post", return_value=mock_response):
        await registry.deploy("deploy-me", vllm_port=8000)

    info = await registry.get("deploy-me")
    assert info.state == AdapterState.DEPLOYED
    assert info.deployed_at is not None


@pytest.mark.asyncio
async def test_deploy_already_deployed(registry, tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    await registry.register("already", "model", str(d))

    mock_response = AsyncMock()
    mock_response.status_code = 200
    with patch.object(registry._http, "post", return_value=mock_response):
        await registry.deploy("already", vllm_port=8000)

    with pytest.raises(AdapterError, match="already deployed"):
        await registry.deploy("already", vllm_port=8000)


@pytest.mark.asyncio
async def test_deploy_vllm_rejects(registry, tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    await registry.register("reject", "model", str(d))

    mock_response = AsyncMock()
    mock_response.status_code = 400
    mock_response.text = "LoRA not compatible"

    with (
        patch.object(registry._http, "post", return_value=mock_response),
        pytest.raises(AdapterError, match="vLLM rejected"),
    ):
        await registry.deploy("reject", vllm_port=8000)


@pytest.mark.asyncio
async def test_undeploy_adapter(registry, tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    await registry.register("undeploy-me", "model", str(d))

    mock_ok = AsyncMock()
    mock_ok.status_code = 200

    with patch.object(registry._http, "post", return_value=mock_ok):
        await registry.deploy("undeploy-me", vllm_port=8000)
        await registry.undeploy("undeploy-me", vllm_port=8000)

    info = await registry.get("undeploy-me")
    assert info.state == AdapterState.AVAILABLE
    assert info.deployed_at is None


@pytest.mark.asyncio
async def test_undeploy_not_deployed(registry, tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    await registry.register("not-deployed", "model", str(d))

    with pytest.raises(AdapterError, match="not deployed"):
        await registry.undeploy("not-deployed", vllm_port=8000)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_adapter(registry, tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    await registry.register("to-delete", "model", str(d))

    assert await registry.delete("to-delete") is True
    assert await registry.get("to-delete") is None


@pytest.mark.asyncio
async def test_delete_nonexistent(registry):
    assert await registry.delete("nope") is False


@pytest.mark.asyncio
async def test_delete_deployed_rejected(registry, tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    await registry.register("deployed", "model", str(d))

    mock_ok = AsyncMock()
    mock_ok.status_code = 200
    with patch.object(registry._http, "post", return_value=mock_ok):
        await registry.deploy("deployed", vllm_port=8000)

    with pytest.raises(AdapterError, match="undeploy first"):
        await registry.delete("deployed")


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_state_with_error(registry, tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    await registry.register("err-adapter", "model", str(d))

    await registry.set_state("err-adapter", AdapterState.ERROR, error_msg="something broke")

    info = await registry.get("err-adapter")
    assert info.state == AdapterState.ERROR
    assert info.metadata.get("error") == "something broke"
