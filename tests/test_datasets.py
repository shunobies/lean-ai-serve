"""Tests for training dataset management."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from lean_ai_serve.config import Settings
from lean_ai_serve.db import Database
from lean_ai_serve.training.datasets import DatasetManager, DatasetValidationError
from lean_ai_serve.training.schemas import DatasetFormat


@pytest_asyncio.fixture
async def db(tmp_path):
    """Create an in-memory database with schema."""
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def dm(db, tmp_path):
    """Create DatasetManager with test settings."""
    settings = Settings()
    settings.training.dataset_directory = str(tmp_path / "datasets")
    settings.training.max_dataset_size_mb = 1  # 1MB for tests
    return DatasetManager(db, settings)


# ---------------------------------------------------------------------------
# ShareGPT format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_sharegpt(dm):
    data = json.dumps([
        {"conversations": [{"from": "human", "value": "Hi"}, {"from": "gpt", "value": "Hello!"}]},
        {"conversations": [{"from": "human", "value": "Bye"}, {"from": "gpt", "value": "Bye!"}]},
    ]).encode()

    info = await dm.upload("test-sg", DatasetFormat.SHAREGPT, data, "user1")
    assert info.name == "test-sg"
    assert info.format == DatasetFormat.SHAREGPT
    assert info.row_count == 2
    assert info.size_bytes == len(data)


@pytest.mark.asyncio
async def test_sharegpt_invalid_no_conversations(dm):
    data = json.dumps([{"text": "missing conversations key"}]).encode()
    with pytest.raises(DatasetValidationError, match="missing 'conversations'"):
        await dm.upload("bad-sg", DatasetFormat.SHAREGPT, data, "user1")


# ---------------------------------------------------------------------------
# Alpaca format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_alpaca(dm):
    data = json.dumps([
        {"instruction": "Summarize this", "input": "Long text...", "output": "Summary."},
    ]).encode()

    info = await dm.upload("test-alp", DatasetFormat.ALPACA, data, "user1")
    assert info.row_count == 1
    assert info.format == DatasetFormat.ALPACA


@pytest.mark.asyncio
async def test_alpaca_missing_output(dm):
    data = json.dumps([{"instruction": "Do something"}]).encode()
    with pytest.raises(DatasetValidationError, match="missing 'output'"):
        await dm.upload("bad-alp", DatasetFormat.ALPACA, data, "user1")


# ---------------------------------------------------------------------------
# JSONL format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_jsonl(dm):
    lines = [
        json.dumps({"prompt": "Q1", "completion": "A1"}),
        json.dumps({"prompt": "Q2", "completion": "A2"}),
        json.dumps({"prompt": "Q3", "completion": "A3"}),
    ]
    data = "\n".join(lines).encode()

    info = await dm.upload("test-jsonl", DatasetFormat.JSONL, data, "user1")
    assert info.row_count == 3


@pytest.mark.asyncio
async def test_jsonl_invalid_line(dm):
    data = b'{"valid": true}\nnot json\n'
    with pytest.raises(DatasetValidationError, match="line 2 invalid JSON"):
        await dm.upload("bad-jsonl", DatasetFormat.JSONL, data, "user1")


# ---------------------------------------------------------------------------
# CSV format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_csv(dm):
    data = b"instruction,output\nDo this,Result\nDo that,Other result\n"
    info = await dm.upload("test-csv", DatasetFormat.CSV, data, "user1")
    assert info.row_count == 2
    assert info.format == DatasetFormat.CSV


@pytest.mark.asyncio
async def test_csv_no_data_rows(dm):
    data = b"col1,col2\n"
    with pytest.raises(DatasetValidationError, match="no data rows"):
        await dm.upload("bad-csv", DatasetFormat.CSV, data, "user1")


# ---------------------------------------------------------------------------
# List, get, delete, preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_datasets(dm):
    data = json.dumps([
        {"instruction": "A", "output": "B"},
    ]).encode()
    await dm.upload("ds1", DatasetFormat.ALPACA, data, "user1")
    await dm.upload("ds2", DatasetFormat.ALPACA, data, "user2")

    datasets = await dm.list_datasets()
    assert len(datasets) == 2
    names = {d.name for d in datasets}
    assert names == {"ds1", "ds2"}


@pytest.mark.asyncio
async def test_get_dataset(dm):
    data = json.dumps([{"instruction": "X", "output": "Y"}]).encode()
    await dm.upload("my-ds", DatasetFormat.ALPACA, data, "user1", description="test desc")

    info = await dm.get("my-ds")
    assert info is not None
    assert info.name == "my-ds"
    assert info.description == "test desc"


@pytest.mark.asyncio
async def test_get_nonexistent(dm):
    assert await dm.get("nope") is None


@pytest.mark.asyncio
async def test_delete_dataset(dm):
    data = json.dumps([{"instruction": "X", "output": "Y"}]).encode()
    await dm.upload("to-delete", DatasetFormat.ALPACA, data, "user1")

    assert await dm.delete("to-delete") is True
    assert await dm.get("to-delete") is None
    assert await dm.delete("to-delete") is False


@pytest.mark.asyncio
async def test_preview(dm):
    data = json.dumps([
        {"instruction": "A", "output": "1"},
        {"instruction": "B", "output": "2"},
        {"instruction": "C", "output": "3"},
    ]).encode()
    await dm.upload("preview-ds", DatasetFormat.ALPACA, data, "user1")

    rows = await dm.preview("preview-ds", limit=2)
    assert len(rows) == 2
    assert rows[0]["instruction"] == "A"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_name_rejected(dm):
    data = json.dumps([{"instruction": "X", "output": "Y"}]).encode()
    await dm.upload("dup", DatasetFormat.ALPACA, data, "user1")

    with pytest.raises(ValueError, match="already exists"):
        await dm.upload("dup", DatasetFormat.ALPACA, data, "user1")


@pytest.mark.asyncio
async def test_size_limit_exceeded(dm):
    # dm is configured with 1MB limit
    huge = b"x" * (2 * 1024 * 1024)
    with pytest.raises(ValueError, match="exceeds max size"):
        await dm.upload("huge", DatasetFormat.JSONL, huge, "user1")


@pytest.mark.asyncio
async def test_preview_nonexistent(dm):
    rows = await dm.preview("nope")
    assert rows == []
