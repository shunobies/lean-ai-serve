"""Tests for training API endpoints."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lean_ai_serve.api.training import router
from lean_ai_serve.config import Settings, set_settings
from lean_ai_serve.db import Database
from lean_ai_serve.models.schemas import AuthUser
from lean_ai_serve.security.auth import authenticate
from lean_ai_serve.training.adapters import AdapterRegistry
from lean_ai_serve.training.backend import TrainingBackend
from lean_ai_serve.training.datasets import DatasetManager
from lean_ai_serve.training.orchestrator import TrainingOrchestrator


def _make_trainer_user() -> AuthUser:
    return AuthUser(
        user_id="trainer1",
        display_name="Trainer",
        roles=["trainer"],
        allowed_models=["*"],
        auth_method="none",
    )


def _make_admin_user() -> AuthUser:
    return AuthUser(
        user_id="admin1",
        display_name="Admin",
        roles=["admin"],
        allowed_models=["*"],
        auth_method="none",
    )


@pytest_asyncio.fixture
async def db(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def app(db, tmp_path):
    """Create a test FastAPI app with training routes and auth bypass."""
    settings = Settings(
        cache={"directory": str(tmp_path / "cache")},
        security={"mode": "none"},
    )
    settings.training.output_directory = str(tmp_path / "outputs")
    settings.training.dataset_directory = str(tmp_path / "datasets")
    settings.training.max_concurrent_jobs = 2
    set_settings(settings)

    dm = DatasetManager(db, settings)
    adapters = AdapterRegistry(db)
    mock_backend = MagicMock(spec=TrainingBackend)
    mock_backend.name = "mock"
    mock_backend.build_config = AsyncMock(return_value={"do_train": True})
    mock_backend.cancel = AsyncMock(return_value=True)

    orchestrator = TrainingOrchestrator(db, settings, mock_backend, dm, adapters)

    # Build app with security mode "none" — authenticate returns admin by default
    test_app = FastAPI()
    test_app.include_router(router)

    # Override authenticate to return admin (bypasses all RBAC)
    test_app.dependency_overrides[authenticate] = lambda: _make_admin_user()

    # Inject deps
    test_app.state.db = db
    test_app.state.dataset_manager = dm
    test_app.state.training_orchestrator = orchestrator
    test_app.state.adapter_registry = adapters
    test_app.state.process_manager = MagicMock()
    test_app.state.process_manager.get_port = MagicMock(return_value=None)

    yield test_app
    await adapters.close()


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Dataset endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_dataset(client):
    data = json.dumps([{"instruction": "X", "output": "Y"}])
    resp = await client.post(
        "/api/training/datasets",
        files={"file": ("data.json", data.encode(), "application/json")},
        data={"name": "test-ds", "format": "alpaca", "description": "test"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "test-ds"
    assert body["format"] == "alpaca"
    assert body["row_count"] == 1


@pytest.mark.asyncio
async def test_upload_invalid_format(client):
    resp = await client.post(
        "/api/training/datasets",
        files={"file": ("data.json", b"content", "application/json")},
        data={"name": "bad", "format": "xml"},
    )
    assert resp.status_code == 400
    assert "Invalid format" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_invalid_content(client):
    resp = await client.post(
        "/api/training/datasets",
        files={"file": ("data.json", b"not valid json", "application/json")},
        data={"name": "bad-content", "format": "alpaca"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_datasets(client):
    data = json.dumps([{"instruction": "A", "output": "B"}])
    await client.post(
        "/api/training/datasets",
        files={"file": ("data.json", data.encode(), "application/json")},
        data={"name": "list-test", "format": "alpaca"},
    )

    resp = await client.get("/api/training/datasets")
    assert resp.status_code == 200
    datasets = resp.json()
    assert len(datasets) >= 1
    assert any(d["name"] == "list-test" for d in datasets)


@pytest.mark.asyncio
async def test_get_dataset(client):
    data = json.dumps([{"instruction": "A", "output": "B"}])
    await client.post(
        "/api/training/datasets",
        files={"file": ("data.json", data.encode(), "application/json")},
        data={"name": "get-test", "format": "alpaca"},
    )

    resp = await client.get("/api/training/datasets/get-test")
    assert resp.status_code == 200
    assert resp.json()["name"] == "get-test"


@pytest.mark.asyncio
async def test_get_dataset_not_found(client):
    resp = await client.get("/api/training/datasets/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_preview_dataset(client):
    data = json.dumps([
        {"instruction": "A", "output": "1"},
        {"instruction": "B", "output": "2"},
        {"instruction": "C", "output": "3"},
    ])
    await client.post(
        "/api/training/datasets",
        files={"file": ("data.json", data.encode(), "application/json")},
        data={"name": "preview-test", "format": "alpaca"},
    )

    resp = await client.get("/api/training/datasets/preview-test/preview?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2


@pytest.mark.asyncio
async def test_delete_dataset(client):
    data = json.dumps([{"instruction": "X", "output": "Y"}])
    await client.post(
        "/api/training/datasets",
        files={"file": ("data.json", data.encode(), "application/json")},
        data={"name": "delete-me", "format": "alpaca"},
    )

    resp = await client.delete("/api/training/datasets/delete-me")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"


@pytest.mark.asyncio
async def test_delete_dataset_not_found(client):
    resp = await client.delete("/api/training/datasets/nonexistent")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Training job endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_job_missing_model(client):
    # Upload dataset first
    data = json.dumps([{"instruction": "X", "output": "Y"}])
    await client.post(
        "/api/training/datasets",
        files={"file": ("data.json", data.encode(), "application/json")},
        data={"name": "job-ds", "format": "alpaca"},
    )

    resp = await client.post(
        "/api/training/jobs",
        json={
            "name": "test-job",
            "base_model": "nonexistent-model",
            "dataset": "job-ds",
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_jobs_empty(client):
    resp = await client.get("/api/training/jobs")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_jobs_invalid_state(client):
    resp = await client.get("/api/training/jobs?state=bogus")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_get_job_not_found(client):
    resp = await client.get("/api/training/jobs/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_gpu_status(client):
    resp = await client.get("/api/training/gpu-status")
    assert resp.status_code == 200
    assert "gpu_assignments" in resp.json()


# ---------------------------------------------------------------------------
# Adapter endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_adapters_empty(client):
    resp = await client.get("/api/training/adapters")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_import_adapter(client, tmp_path):
    adapter_dir = tmp_path / "ext_adapter"
    adapter_dir.mkdir()

    resp = await client.post(
        "/api/training/adapters/import",
        json={
            "name": "imported",
            "base_model": "llama-3",
            "path": str(adapter_dir),
            "metadata": {"source": "external"},
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "imported"
    assert body["state"] == "available"


@pytest.mark.asyncio
async def test_import_duplicate_adapter(client, tmp_path):
    adapter_dir = tmp_path / "ext"
    adapter_dir.mkdir()

    await client.post(
        "/api/training/adapters/import",
        json={"name": "dup", "base_model": "model", "path": str(adapter_dir)},
    )

    resp = await client.post(
        "/api/training/adapters/import",
        json={"name": "dup", "base_model": "model", "path": str(adapter_dir)},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_import_nonexistent_path(client):
    resp = await client.post(
        "/api/training/adapters/import",
        json={
            "name": "bad-path",
            "base_model": "model",
            "path": "/nonexistent/path",
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_get_adapter_not_found(client):
    resp = await client.get("/api/training/adapters/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_deploy_adapter_model_not_loaded(client, tmp_path):
    """Deploy fails when the target model is not loaded."""
    adapter_dir = tmp_path / "deploy_adapter"
    adapter_dir.mkdir()
    await client.post(
        "/api/training/adapters/import",
        json={"name": "deploy-test", "base_model": "model", "path": str(adapter_dir)},
    )

    resp = await client.post(
        "/api/training/adapters/deploy-test/deploy",
        json={"model_name": "some-model"},
    )
    assert resp.status_code == 400
    assert "not loaded" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_delete_adapter(client, tmp_path):
    adapter_dir = tmp_path / "del"
    adapter_dir.mkdir()
    await client.post(
        "/api/training/adapters/import",
        json={"name": "del-me", "base_model": "model", "path": str(adapter_dir)},
    )

    resp = await client.delete("/api/training/adapters/del-me")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"


@pytest.mark.asyncio
async def test_delete_adapter_not_found(client):
    resp = await client.delete("/api/training/adapters/nonexistent")
    assert resp.status_code == 404
