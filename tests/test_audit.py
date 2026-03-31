"""Tests for the audit logging system."""

from __future__ import annotations

import pytest

from lean_ai_serve.config import Settings, set_settings
from lean_ai_serve.db import Database
from lean_ai_serve.security.audit import AuditLogger


@pytest.fixture
async def audit(tmp_path) -> AuditLogger:
    set_settings(Settings(
        cache={"directory": str(tmp_path)},
        audit={"enabled": True, "log_prompts": True, "log_prompts_hash_only": False},
    ))
    db = Database(tmp_path / "test.db")
    await db.connect()
    a = AuditLogger(db)
    await a.initialize()
    yield a
    await db.close()


async def test_log_and_query(audit: AuditLogger):
    """Should log an entry and retrieve it."""
    await audit.log(
        user_id="test-user",
        action="inference",
        model="test-model",
        prompt="Hello",
        response="Hi there",
        token_count=10,
        latency_ms=50,
    )

    entries, total = await audit.query(user_id="test-user")
    assert total == 1
    assert entries[0]["action"] == "inference"
    assert entries[0]["model"] == "test-model"


async def test_hash_chain(audit: AuditLogger):
    """Hash chain should be valid after multiple entries."""
    for i in range(5):
        await audit.log(
            user_id=f"user-{i}",
            action="inference",
            model="test",
        )

    is_valid, message = await audit.verify_chain()
    assert is_valid is True
    assert "5 entries OK" in message


async def test_query_filters(audit: AuditLogger):
    """Query should filter by action and model."""
    await audit.log(user_id="u1", action="inference", model="m1")
    await audit.log(user_id="u1", action="model_load", model="m2")
    await audit.log(user_id="u2", action="inference", model="m1")

    entries, total = await audit.query(action="inference")
    assert total == 2

    entries, total = await audit.query(model="m2")
    assert total == 1


async def test_disabled_audit(tmp_path):
    """When audit is disabled, nothing should be logged."""
    set_settings(Settings(
        cache={"directory": str(tmp_path)},
        audit={"enabled": False},
    ))
    db = Database(tmp_path / "test.db")
    await db.connect()
    a = AuditLogger(db)
    await a.initialize()

    await a.log(user_id="u1", action="test")

    entries, total = await a.query()
    assert total == 0
    await db.close()


async def test_hash_only_mode(tmp_path):
    """Hash-only mode should store hashes but not content."""
    set_settings(Settings(
        cache={"directory": str(tmp_path)},
        audit={
            "enabled": True,
            "log_prompts": True,
            "log_prompts_hash_only": True,
        },
    ))
    db = Database(tmp_path / "test.db")
    await db.connect()
    a = AuditLogger(db)
    await a.initialize()

    await a.log(user_id="u1", action="inference", prompt="secret data")

    row = await db.fetchone("SELECT * FROM audit_log WHERE user_id = ?", ("u1",))
    assert row["prompt_content"] is None  # Content not stored
    assert row["prompt_hash"] is not None  # But hash is
    await db.close()
