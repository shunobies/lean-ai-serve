"""Tests for usage tracking."""

from __future__ import annotations

import pytest
import pytest_asyncio

from lean_ai_serve.db import Database
from lean_ai_serve.security.usage import UsageTracker


@pytest_asyncio.fixture
async def db(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def tracker(db):
    return UsageTracker(db)


# ---------------------------------------------------------------------------
# record() — hourly bucket insertion / accumulation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_creates_bucket(tracker, db):
    await tracker.record(
        user_id="alice", model="llama-3", prompt_tokens=10,
        completion_tokens=20, latency_ms=100,
    )
    rows = await db.fetchall("SELECT * FROM usage")
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == "alice"
    assert row["model"] == "llama-3"
    assert row["request_count"] == 1
    assert row["prompt_tokens"] == 10
    assert row["completion_tokens"] == 20
    assert row["total_latency_ms"] == 100


@pytest.mark.asyncio
async def test_record_accumulates_same_hour(tracker, db):
    for _ in range(3):
        await tracker.record(
            user_id="bob", model="mistral", prompt_tokens=5,
            completion_tokens=10, latency_ms=50,
        )
    rows = await db.fetchall("SELECT * FROM usage")
    assert len(rows) == 1
    row = rows[0]
    assert row["request_count"] == 3
    assert row["prompt_tokens"] == 15
    assert row["completion_tokens"] == 30
    assert row["total_latency_ms"] == 150


@pytest.mark.asyncio
async def test_different_users_separate_rows(tracker, db):
    await tracker.record(user_id="alice", model="m1", prompt_tokens=1)
    await tracker.record(user_id="bob", model="m1", prompt_tokens=2)
    rows = await db.fetchall("SELECT * FROM usage")
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_different_models_separate_rows(tracker, db):
    await tracker.record(user_id="alice", model="m1", prompt_tokens=1)
    await tracker.record(user_id="alice", model="m2", prompt_tokens=2)
    rows = await db.fetchall("SELECT * FROM usage")
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# query()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_all(tracker):
    await tracker.record(user_id="alice", model="m1", prompt_tokens=10)
    await tracker.record(user_id="bob", model="m2", prompt_tokens=20)
    results = await tracker.query()
    assert len(results) == 2


@pytest.mark.asyncio
async def test_query_by_user(tracker):
    await tracker.record(user_id="alice", model="m1", prompt_tokens=10)
    await tracker.record(user_id="bob", model="m1", prompt_tokens=20)
    results = await tracker.query(user_id="alice")
    assert len(results) == 1
    assert results[0]["user_id"] == "alice"


@pytest.mark.asyncio
async def test_query_by_model(tracker):
    await tracker.record(user_id="alice", model="m1", prompt_tokens=10)
    await tracker.record(user_id="alice", model="m2", prompt_tokens=20)
    results = await tracker.query(model="m2")
    assert len(results) == 1
    assert results[0]["model"] == "m2"


@pytest.mark.asyncio
async def test_query_time_range(tracker, db):
    # Insert records with explicit hours
    await db.execute(
        """
        INSERT INTO usage (hour, user_id, model, request_count,
                           prompt_tokens, completion_tokens, total_latency_ms)
        VALUES (?, ?, ?, 1, 10, 20, 100)
        """,
        ("2025-01-01T10:00:00", "alice", "m1"),
    )
    await db.execute(
        """
        INSERT INTO usage (hour, user_id, model, request_count,
                           prompt_tokens, completion_tokens, total_latency_ms)
        VALUES (?, ?, ?, 1, 10, 20, 100)
        """,
        ("2025-01-01T14:00:00", "alice", "m1"),
    )
    await db.commit()

    results = await tracker.query(
        from_hour="2025-01-01T12:00:00", to_hour="2025-01-01T15:00:00"
    )
    assert len(results) == 1
    assert results[0]["hour"] == "2025-01-01T14:00:00"


@pytest.mark.asyncio
async def test_query_limit(tracker):
    for i in range(5):
        await tracker.record(user_id=f"user-{i}", model="m1", prompt_tokens=1)
    results = await tracker.query(limit=3)
    assert len(results) == 3


# ---------------------------------------------------------------------------
# get_user_summary()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_summary(tracker):
    await tracker.record(
        user_id="alice", model="m1", prompt_tokens=10,
        completion_tokens=20, latency_ms=100,
    )
    await tracker.record(
        user_id="alice", model="m2", prompt_tokens=5,
        completion_tokens=15, latency_ms=50,
    )
    summary = await tracker.get_user_summary("alice", period_hours=24)
    assert summary["user_id"] == "alice"
    assert summary["request_count"] == 2
    assert summary["prompt_tokens"] == 15
    assert summary["completion_tokens"] == 35
    assert summary["total_latency_ms"] == 150
    assert set(summary["models_used"]) == {"m1", "m2"}


@pytest.mark.asyncio
async def test_user_summary_empty(tracker):
    summary = await tracker.get_user_summary("nobody", period_hours=24)
    assert summary["request_count"] == 0
    assert summary["models_used"] == []


# ---------------------------------------------------------------------------
# get_model_summary()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_summary(tracker):
    await tracker.record(
        user_id="alice", model="m1", prompt_tokens=10,
        completion_tokens=20, latency_ms=100,
    )
    await tracker.record(
        user_id="bob", model="m1", prompt_tokens=5,
        completion_tokens=15, latency_ms=50,
    )
    summary = await tracker.get_model_summary("m1", period_hours=24)
    assert summary["model"] == "m1"
    assert summary["request_count"] == 2
    assert summary["prompt_tokens"] == 15
    assert summary["unique_users"] == 2


# ---------------------------------------------------------------------------
# cleanup()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup(tracker, db):
    # Insert an old record directly
    await db.execute(
        """
        INSERT INTO usage (hour, user_id, model, request_count,
                           prompt_tokens, completion_tokens, total_latency_ms)
        VALUES (?, ?, ?, 1, 10, 20, 100)
        """,
        ("2020-01-01T00:00:00", "old-user", "old-model"),
    )
    await db.commit()

    # Also add a recent record via the tracker
    await tracker.record(user_id="alice", model="m1", prompt_tokens=1)

    deleted = await tracker.cleanup(retention_days=90)
    assert deleted == 1

    # Recent record should survive
    rows = await db.fetchall("SELECT * FROM usage")
    assert len(rows) == 1
    assert rows[0]["user_id"] == "alice"
