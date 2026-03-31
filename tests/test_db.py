"""Tests for the database layer."""

from __future__ import annotations

import pytest

from lean_ai_serve.db import Database


@pytest.fixture
async def test_db(tmp_path) -> Database:
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


async def test_connect_creates_tables(test_db: Database):
    """connect() should create all schema tables."""
    tables = await test_db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    names = {row["name"] for row in tables}
    assert "models" in names
    assert "api_keys" in names
    assert "audit_log" in names
    assert "usage" in names
    assert "adapters" in names
    assert "training_jobs" in names
    assert "datasets" in names


async def test_execute_and_fetch(test_db: Database):
    """Basic insert and fetch should work."""
    await test_db.execute(
        "INSERT INTO models (name, source, state) VALUES (?, ?, ?)",
        ("test", "org/test", "not_downloaded"),
    )
    await test_db.commit()

    row = await test_db.fetchone("SELECT * FROM models WHERE name = ?", ("test",))
    assert row is not None
    assert row["name"] == "test"
    assert row["source"] == "org/test"


async def test_fetchall(test_db: Database):
    """fetchall should return all matching rows."""
    for i in range(3):
        await test_db.execute(
            "INSERT INTO models (name, source, state) VALUES (?, ?, ?)",
            (f"model-{i}", "org/test", "not_downloaded"),
        )
    await test_db.commit()

    rows = await test_db.fetchall("SELECT * FROM models")
    assert len(rows) == 3
