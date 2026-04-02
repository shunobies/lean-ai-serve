"""Tests for the database layer."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from lean_ai_serve.db import Database, models_table, usage_table


@pytest.fixture
async def test_db(tmp_path) -> Database:
    url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    db = Database(url)
    await db.connect()
    yield db
    await db.close()


async def test_connect_creates_tables(test_db: Database):
    """connect() should create all schema tables."""
    async with test_db.engine.connect() as conn:
        table_names = await conn.run_sync(
            lambda sync_conn: sa.inspect(sync_conn).get_table_names()
        )
    names = set(table_names)
    assert "models" in names
    assert "api_keys" in names
    assert "audit_log" in names
    assert "usage" in names
    assert "adapters" in names
    assert "training_jobs" in names
    assert "datasets" in names
    assert "revoked_tokens" in names


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


async def test_upsert_replace(test_db: Database):
    """upsert with on_conflict='replace' should update existing row."""
    await test_db.upsert(
        models_table,
        {"name": "m1", "source": "org/a", "state": "not_downloaded"},
        on_conflict="replace",
    )
    await test_db.commit()

    await test_db.upsert(
        models_table,
        {"name": "m1", "source": "org/b", "state": "downloaded"},
        on_conflict="replace",
    )
    await test_db.commit()

    row = await test_db.fetchone("SELECT * FROM models WHERE name = ?", ("m1",))
    assert row["source"] == "org/b"
    assert row["state"] == "downloaded"


async def test_upsert_ignore(test_db: Database):
    """upsert with on_conflict='ignore' should not update existing row."""
    await test_db.execute(
        "INSERT INTO models (name, source, state) VALUES (?, ?, ?)",
        ("m1", "org/a", "not_downloaded"),
    )
    await test_db.commit()

    await test_db.upsert(
        models_table,
        {"name": "m1", "source": "org/b", "state": "downloaded"},
        on_conflict="ignore",
    )
    await test_db.commit()

    row = await test_db.fetchone("SELECT * FROM models WHERE name = ?", ("m1",))
    assert row["source"] == "org/a"  # unchanged


async def test_upsert_increment(test_db: Database):
    """upsert_increment should increment values on conflict."""
    await test_db.upsert_increment(
        usage_table,
        values={
            "hour": "2025-01-01T00:00:00",
            "user_id": "alice",
            "model": "llama",
            "request_count": 1,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_latency_ms": 100,
        },
        conflict_columns=["hour", "user_id", "model"],
        increment_columns={
            "request_count": 1,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_latency_ms": 100,
        },
    )
    await test_db.commit()

    # Second call — should increment
    await test_db.upsert_increment(
        usage_table,
        values={
            "hour": "2025-01-01T00:00:00",
            "user_id": "alice",
            "model": "llama",
            "request_count": 1,
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "total_latency_ms": 200,
        },
        conflict_columns=["hour", "user_id", "model"],
        increment_columns={
            "request_count": 1,
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "total_latency_ms": 200,
        },
    )
    await test_db.commit()

    row = await test_db.fetchone(
        "SELECT * FROM usage WHERE hour = ? AND user_id = ? AND model = ?",
        ("2025-01-01T00:00:00", "alice", "llama"),
    )
    assert row["request_count"] == 2
    assert row["prompt_tokens"] == 30
    assert row["completion_tokens"] == 15
    assert row["total_latency_ms"] == 300


async def test_dialect_property(test_db: Database):
    """dialect should return 'sqlite' for SQLite databases."""
    assert test_db.dialect == "sqlite"


async def test_database_url(test_db: Database):
    """url property should return the connection URL."""
    assert "sqlite" in test_db.url
