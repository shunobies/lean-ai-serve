"""Tests for authentication and API key management."""

from __future__ import annotations

import pytest

from lean_ai_serve.db import Database
from lean_ai_serve.security.auth import (
    authenticate_api_key,
    create_api_key,
    decode_jwt,
    generate_api_key,
    hash_api_key,
    issue_jwt,
    verify_api_key,
)


@pytest.fixture
async def auth_db(tmp_path) -> Database:
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


def test_generate_api_key():
    """Generated keys should have the las- prefix."""
    key = generate_api_key()
    assert key.startswith("las-")
    assert len(key) > 20


def test_hash_and_verify():
    """Bcrypt hash/verify roundtrip should work."""
    key = "las-test-key-12345"
    hashed = hash_api_key(key)
    assert verify_api_key(key, hashed) is True
    assert verify_api_key("wrong-key", hashed) is False


async def test_create_and_authenticate(auth_db: Database):
    """Should create an API key and authenticate with it."""
    from lean_ai_serve.config import Settings, set_settings

    set_settings(Settings(security={"mode": "api_key"}))

    key_id, raw_key = await create_api_key(
        auth_db, name="test-key", role="user", models=["model-a"]
    )
    assert key_id
    assert raw_key.startswith("las-")

    user = await authenticate_api_key(auth_db, raw_key)
    assert user is not None
    assert user.user_id == "test-key"
    assert user.roles == ["user"]
    assert "model-a" in user.allowed_models


async def test_authenticate_invalid_key(auth_db: Database):
    """Invalid key should return None."""
    user = await authenticate_api_key(auth_db, "las-invalid-key")
    assert user is None


def test_jwt_roundtrip():
    """JWT issue/decode should preserve claims."""
    from lean_ai_serve.config import Settings, set_settings

    set_settings(Settings(security={"jwt_secret": "test-secret-123"}))

    token = issue_jwt("user1", "User One", ["admin"], ["*"])
    payload = decode_jwt(token)
    assert payload is not None
    assert payload["sub"] == "user1"
    assert payload["name"] == "User One"
    assert payload["roles"] == ["admin"]


def test_jwt_invalid_token():
    """Invalid JWT should return None."""
    from lean_ai_serve.config import Settings, set_settings

    set_settings(Settings(security={"jwt_secret": "test-secret-123"}))

    assert decode_jwt("not-a-jwt") is None
