"""Tests for training orchestrator."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from lean_ai_serve.config import Settings
from lean_ai_serve.db import Database
from lean_ai_serve.training.adapters import AdapterRegistry
from lean_ai_serve.training.backend import TrainingBackend
from lean_ai_serve.training.datasets import DatasetManager
from lean_ai_serve.training.orchestrator import TrainingOrchestrator
from lean_ai_serve.training.schemas import (
    DatasetFormat,
    TrainingJobState,
    TrainingSubmitRequest,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.training.output_directory = str(tmp_path / "outputs")
    s.training.dataset_directory = str(tmp_path / "datasets")
    s.training.max_concurrent_jobs = 2
    s.training.default_gpu = [0]
    return s


@pytest_asyncio.fixture
async def dm(db, settings):
    return DatasetManager(db, settings)


@pytest_asyncio.fixture
async def adapters(db):
    reg = AdapterRegistry(db)
    yield reg
    await reg.close()


@pytest.fixture
def mock_backend():
    backend = MagicMock(spec=TrainingBackend)
    backend.name = "mock"
    backend.build_config = AsyncMock(return_value={"model": "test", "do_train": True})
    backend.cancel = AsyncMock(return_value=True)
    return backend


@pytest_asyncio.fixture
async def orchestrator(db, settings, mock_backend, dm, adapters):
    return TrainingOrchestrator(db, settings, mock_backend, dm, adapters)


async def _setup_model(db, name="test-model", state="downloaded"):
    """Insert a test model into the DB."""
    await db.execute(
        """
        INSERT INTO models (name, source, state, config_json)
        VALUES (?, ?, ?, ?)
        """,
        (name, "org/model", state, json.dumps({"source": "org/model"})),
    )
    await db.commit()


async def _setup_dataset(dm, name="test-data"):
    """Upload a test dataset."""
    data = json.dumps([{"instruction": "test", "output": "result"}]).encode()
    await dm.upload(name, DatasetFormat.ALPACA, data, "user1")


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_job(orchestrator, db, dm):
    await _setup_model(db)
    await _setup_dataset(dm)

    request = TrainingSubmitRequest(
        name="my-training",
        base_model="test-model",
        dataset="test-data",
    )

    job = await orchestrator.submit(request, submitted_by="user1")

    assert job.name == "my-training"
    assert job.base_model == "test-model"
    assert job.dataset == "test-data"
    assert job.state == TrainingJobState.QUEUED
    assert job.submitted_by == "user1"
    assert job.adapter_name == "test-model-my-training"
    assert job.gpu == [0]


@pytest.mark.asyncio
async def test_submit_custom_gpu_and_adapter(orchestrator, db, dm):
    await _setup_model(db)
    await _setup_dataset(dm)

    request = TrainingSubmitRequest(
        name="custom",
        base_model="test-model",
        dataset="test-data",
        gpu=[1, 2],
        adapter_name="my-custom-adapter",
    )

    job = await orchestrator.submit(request, submitted_by="user1")
    assert job.gpu == [1, 2]
    assert job.adapter_name == "my-custom-adapter"


@pytest.mark.asyncio
async def test_submit_missing_dataset(orchestrator, db):
    await _setup_model(db)

    request = TrainingSubmitRequest(
        name="fail",
        base_model="test-model",
        dataset="nonexistent",
    )

    with pytest.raises(ValueError, match="not found"):
        await orchestrator.submit(request, submitted_by="user1")


@pytest.mark.asyncio
async def test_submit_model_not_downloaded(orchestrator, db, dm):
    await _setup_model(db, state="not_downloaded")
    await _setup_dataset(dm)

    request = TrainingSubmitRequest(
        name="fail",
        base_model="test-model",
        dataset="test-data",
    )

    with pytest.raises(ValueError, match="must be downloaded"):
        await orchestrator.submit(request, submitted_by="user1")


@pytest.mark.asyncio
async def test_submit_model_not_found(orchestrator, dm):
    await _setup_dataset(dm)

    request = TrainingSubmitRequest(
        name="fail",
        base_model="nonexistent",
        dataset="test-data",
    )

    with pytest.raises(ValueError, match="not found"):
        await orchestrator.submit(request, submitted_by="user1")


# ---------------------------------------------------------------------------
# GPU conflicts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gpu_conflict_detected(orchestrator, db, dm):
    await _setup_model(db)
    await _setup_dataset(dm, "ds1")
    await _setup_dataset(dm, "ds2")

    r1 = TrainingSubmitRequest(
        name="job1", base_model="test-model", dataset="ds1", gpu=[0],
    )
    job1 = await orchestrator.submit(r1, submitted_by="user1")

    # Simulate GPU lock (normally set during stream_progress)
    orchestrator._gpu_locks[0] = job1.id

    r2 = TrainingSubmitRequest(
        name="job2", base_model="test-model", dataset="ds2", gpu=[0],
    )
    with pytest.raises(ValueError, match="already in use"):
        await orchestrator.submit(r2, submitted_by="user1")


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_jobs(orchestrator, db, dm):
    await _setup_model(db)
    await _setup_dataset(dm, "d1")
    await _setup_dataset(dm, "d2")

    r1 = TrainingSubmitRequest(name="j1", base_model="test-model", dataset="d1")
    r2 = TrainingSubmitRequest(name="j2", base_model="test-model", dataset="d2")

    await orchestrator.submit(r1, submitted_by="user1")
    await orchestrator.submit(r2, submitted_by="user2")

    all_jobs = await orchestrator.list_jobs()
    assert len(all_jobs) == 2

    queued = await orchestrator.list_jobs(state=TrainingJobState.QUEUED)
    assert len(queued) == 2


@pytest.mark.asyncio
async def test_get_job(orchestrator, db, dm):
    await _setup_model(db)
    await _setup_dataset(dm)

    request = TrainingSubmitRequest(
        name="get-me", base_model="test-model", dataset="test-data",
    )
    job = await orchestrator.submit(request, submitted_by="user1")

    fetched = await orchestrator.get_job(job.id)
    assert fetched is not None
    assert fetched.name == "get-me"

    assert await orchestrator.get_job("nonexistent") is None


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_queued_job(orchestrator, db, dm):
    await _setup_model(db)
    await _setup_dataset(dm)

    request = TrainingSubmitRequest(
        name="cancel-me", base_model="test-model", dataset="test-data",
    )
    job = await orchestrator.submit(request, submitted_by="user1")

    cancelled = await orchestrator.cancel_job(job.id)
    assert cancelled is True

    updated = await orchestrator.get_job(job.id)
    assert updated.state == TrainingJobState.CANCELLED


@pytest.mark.asyncio
async def test_cancel_nonexistent(orchestrator):
    assert await orchestrator.cancel_job("nope") is False


# ---------------------------------------------------------------------------
# GPU usage tracking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gpu_usage(orchestrator):
    assert orchestrator.get_gpu_usage() == {}

    orchestrator._gpu_locks[0] = "job-1"
    orchestrator._gpu_locks[1] = "job-2"

    usage = orchestrator.get_gpu_usage()
    assert usage == {0: "job-1", 1: "job-2"}


# ---------------------------------------------------------------------------
# Max concurrent jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_concurrent_jobs(orchestrator, db, dm):
    await _setup_model(db)
    await _setup_dataset(dm, "d1")
    await _setup_dataset(dm, "d2")
    await _setup_dataset(dm, "d3")

    # Submit 2 jobs (max_concurrent_jobs=2)
    r1 = TrainingSubmitRequest(name="j1", base_model="test-model", dataset="d1", gpu=[0])
    r2 = TrainingSubmitRequest(name="j2", base_model="test-model", dataset="d2", gpu=[1])
    await orchestrator.submit(r1, submitted_by="user1")
    await orchestrator.submit(r2, submitted_by="user1")

    # Third should be rejected
    r3 = TrainingSubmitRequest(name="j3", base_model="test-model", dataset="d3", gpu=[2])
    with pytest.raises(ValueError, match="Max concurrent"):
        await orchestrator.submit(r3, submitted_by="user1")
