"""Tests for CLI admin commands."""

from __future__ import annotations

import pytest

from lean_ai_serve.config import Settings, set_settings
from lean_ai_serve.db import Database
from lean_ai_serve.security.audit import AuditLogger


@pytest.fixture
async def admin_db(tmp_path):
    """Create a test database with audit data."""
    settings = Settings(cache={"directory": str(tmp_path)})
    set_settings(settings)

    db = Database(tmp_path / "lean_ai_serve.db")
    await db.connect()

    # Populate some audit entries
    audit = AuditLogger(db)
    await audit.initialize()
    for i in range(5):
        await audit.log(
            user_id=f"user-{i}",
            action="inference",
            model="test-model",
            prompt=f"prompt-{i}",
            status="success",
        )

    yield db, audit, tmp_path
    await db.close()
    set_settings(None)


@pytest.fixture(autouse=True)
def _clear_settings():
    yield
    set_settings(None)


# ---------------------------------------------------------------------------
# audit-verify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_verify_intact_chain(admin_db):
    """Intact hash chain verifies successfully."""
    _, audit, _ = admin_db
    valid, msg = await audit.verify_chain()
    assert valid is True
    assert "5 entries OK" in msg


@pytest.mark.asyncio
async def test_audit_verify_empty_db(tmp_path):
    """Empty audit log verifies successfully."""
    settings = Settings(cache={"directory": str(tmp_path)})
    set_settings(settings)

    db = Database(tmp_path / "lean_ai_serve.db")
    await db.connect()
    audit = AuditLogger(db)
    await audit.initialize()

    valid, msg = await audit.verify_chain()
    await db.close()

    assert valid is True
    assert "No audit entries" in msg


@pytest.mark.asyncio
async def test_audit_verify_tampered_chain(admin_db):
    """Tampered chain hash is detected."""
    db, audit, _ = admin_db

    # Tamper with a chain hash
    await db.execute(
        "UPDATE audit_log SET chain_hash = 'tampered' WHERE id = 3"
    )
    await db.commit()

    valid, msg = await audit.verify_chain()
    assert valid is False
    assert "Chain broken" in msg


# ---------------------------------------------------------------------------
# audit-export
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_export_json(admin_db):
    """Audit query returns entries as dicts."""
    _, audit, _ = admin_db
    entries, total = await audit.query(limit=10)
    assert total == 5
    assert len(entries) == 5
    assert entries[0]["action"] == "inference"


@pytest.mark.asyncio
async def test_audit_export_with_time_filter(admin_db):
    """Audit query with time filter works."""
    from datetime import UTC, datetime, timedelta

    _, audit, _ = admin_db
    future = datetime.now(UTC) + timedelta(hours=1)
    entries, total = await audit.query(from_time=future, limit=10)
    assert total == 0
    assert len(entries) == 0


# ---------------------------------------------------------------------------
# db-stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_stats(admin_db):
    """Database tables have expected row counts."""
    db, _, _ = admin_db

    row = await db.fetchone("SELECT COUNT(*) as cnt FROM audit_log")
    assert row["cnt"] == 5

    row = await db.fetchone("SELECT COUNT(*) as cnt FROM models")
    assert row["cnt"] == 0


# ---------------------------------------------------------------------------
# token-cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_cleanup_no_expired(admin_db):
    """Token cleanup with no expired tokens returns 0."""
    from lean_ai_serve.security.auth import cleanup_revoked_tokens

    db, _, _ = admin_db
    removed = await cleanup_revoked_tokens(db)
    assert removed == 0


@pytest.mark.asyncio
async def test_token_cleanup_removes_expired(admin_db):
    """Token cleanup removes expired revoked tokens."""
    from datetime import UTC, datetime, timedelta

    from lean_ai_serve.security.auth import cleanup_revoked_tokens

    db, _, _ = admin_db

    # Insert some expired revoked tokens
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    for i in range(3):
        await db.execute(
            "INSERT INTO revoked_tokens (jti, user_id, revoked_at, expires_at) VALUES (?, ?, ?, ?)",
            (f"jti-{i}", "user-test", past, past),
        )
    await db.commit()

    removed = await cleanup_revoked_tokens(db)
    assert removed == 3
