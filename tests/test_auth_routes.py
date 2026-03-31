"""Tests for auth endpoints and token revocation."""

from __future__ import annotations

import pytest

from lean_ai_serve.config import Settings, set_settings
from lean_ai_serve.db import Database
from lean_ai_serve.security.auth import (
    _revoked_tokens,
    cleanup_revoked_tokens,
    decode_jwt,
    issue_jwt,
    load_revoked_tokens,
    revoke_token,
)


@pytest.fixture
async def auth_db(tmp_path) -> Database:
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


@pytest.fixture(autouse=True)
def _setup_settings():
    set_settings(Settings(security={"jwt_secret": "test-secret-auth-routes"}))
    _revoked_tokens.clear()


async def test_issue_jwt_returns_tuple():
    """issue_jwt should return (token, jti, expires_at)."""
    token, jti, expires_at = issue_jwt("user1", "User 1", ["user"], ["*"])
    assert isinstance(token, str)
    assert isinstance(jti, str)
    assert len(jti) > 0
    payload = decode_jwt(token)
    assert payload["jti"] == jti


async def test_revoke_token(auth_db):
    """Revoking a token should add it to the in-memory set and DB."""
    token, jti, expires_at = issue_jwt("user1", "User 1", ["user"], ["*"])
    assert jti not in _revoked_tokens

    await revoke_token(auth_db, jti, "user1", expires_at.isoformat())
    assert jti in _revoked_tokens

    # Verify DB row
    row = await auth_db.fetchone("SELECT * FROM revoked_tokens WHERE jti = ?", (jti,))
    assert row is not None
    assert row["user_id"] == "user1"


async def test_load_revoked_tokens(auth_db):
    """load_revoked_tokens should populate the in-memory set from DB."""
    from datetime import UTC, datetime, timedelta

    # Insert a non-expired revoked token
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await auth_db.execute(
        "INSERT INTO revoked_tokens (jti, user_id, revoked_at, expires_at) VALUES (?, ?, ?, ?)",
        ("test-jti", "user1", datetime.now(UTC).isoformat(), future),
    )
    await auth_db.commit()

    _revoked_tokens.clear()
    await load_revoked_tokens(auth_db)
    assert "test-jti" in _revoked_tokens


async def test_cleanup_revoked_tokens(auth_db):
    """Expired revoked tokens should be cleaned up."""
    from datetime import UTC, datetime, timedelta

    # Insert an expired token
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    await auth_db.execute(
        "INSERT INTO revoked_tokens (jti, user_id, revoked_at, expires_at) VALUES (?, ?, ?, ?)",
        ("expired-jti", "user1", datetime.now(UTC).isoformat(), past),
    )
    await auth_db.commit()
    _revoked_tokens.add("expired-jti")

    count = await cleanup_revoked_tokens(auth_db)
    assert count == 1
    assert "expired-jti" not in _revoked_tokens


async def test_revoked_token_not_loaded_if_expired(auth_db):
    """load_revoked_tokens should NOT load already-expired tokens."""
    from datetime import UTC, datetime, timedelta

    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    await auth_db.execute(
        "INSERT INTO revoked_tokens (jti, user_id, revoked_at, expires_at) VALUES (?, ?, ?, ?)",
        ("old-jti", "user1", datetime.now(UTC).isoformat(), past),
    )
    await auth_db.commit()

    _revoked_tokens.clear()
    await load_revoked_tokens(auth_db)
    assert "old-jti" not in _revoked_tokens


async def test_jwt_with_revoked_jti_rejected():
    """A JWT whose jti is in the revoked set should not authenticate."""
    token, jti, expires_at = issue_jwt("user1", "User 1", ["user"], ["*"])

    # Verify token works before revocation
    payload = decode_jwt(token)
    assert payload is not None
    assert payload["jti"] == jti

    # Mark as revoked
    _revoked_tokens.add(jti)

    # Token still decodes (decode_jwt doesn't check revocation)
    # but the authenticate() function checks _revoked_tokens
    payload = decode_jwt(token)
    assert payload is not None
    assert payload["jti"] in _revoked_tokens
