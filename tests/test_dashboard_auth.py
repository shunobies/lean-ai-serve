"""Tests for dashboard authentication and CSRF."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from lean_ai_serve.config import DashboardConfig, Settings, set_settings
from lean_ai_serve.dashboard.dependencies import (
    SESSION_COOKIE,
    generate_csrf_token,
    get_dashboard_user,
    verify_csrf_token,
)
from lean_ai_serve.security.auth import issue_jwt


@pytest.fixture(autouse=True)
def _configure():
    import lean_ai_serve.dashboard.dependencies as deps
    deps._csrf_secret = None
    deps._templates = None
    settings = Settings(
        security={"mode": "api_key", "jwt_secret": "test-secret-key-for-jwt-signing"},
        dashboard=DashboardConfig(enabled=True, session_secret="test-csrf-secret"),
    )
    set_settings(settings)
    yield
    set_settings(None)
    deps._csrf_secret = None
    deps._templates = None


class TestCSRF:
    def test_generate_csrf_token(self):
        token = generate_csrf_token("test-jti-123")
        assert isinstance(token, str)
        assert len(token) == 48

    def test_csrf_token_deterministic(self):
        token1 = generate_csrf_token("same-jti")
        token2 = generate_csrf_token("same-jti")
        assert token1 == token2

    def test_csrf_token_different_jti(self):
        token1 = generate_csrf_token("jti-1")
        token2 = generate_csrf_token("jti-2")
        assert token1 != token2

    def test_verify_csrf_valid(self):
        jti = "test-jti-456"
        token = generate_csrf_token(jti)
        assert verify_csrf_token(token, jti)

    def test_verify_csrf_invalid(self):
        assert not verify_csrf_token("bad-token", "test-jti")


class TestSessionAuth:
    async def test_no_cookie_returns_none(self):
        request = AsyncMock()
        request.cookies = {}
        user = await get_dashboard_user(request)
        assert user is None

    async def test_invalid_token_returns_none(self):
        request = AsyncMock()
        request.cookies = {SESSION_COOKIE: "invalid-jwt-token"}
        user = await get_dashboard_user(request)
        assert user is None

    async def test_valid_token_returns_user(self):
        token, _jti, _exp = issue_jwt("testuser", "Test User", ["admin"], ["*"])
        request = AsyncMock()
        request.cookies = {SESSION_COOKIE: token}
        user = await get_dashboard_user(request)
        assert user is not None
        assert user.user_id == "testuser"
        assert user.display_name == "Test User"
        assert "admin" in user.roles

    async def test_expired_token_returns_none(self):
        """Expired JWTs should return None."""
        from datetime import UTC, datetime, timedelta

        import jwt as pyjwt

        payload = {
            "sub": "expired-user",
            "name": "Expired",
            "roles": ["user"],
            "models": ["*"],
            "jti": "expired-jti",
            "iat": datetime.now(UTC) - timedelta(hours=24),
            "exp": datetime.now(UTC) - timedelta(hours=1),
            "iss": "lean-ai-serve",
        }
        token = pyjwt.encode(payload, "test-secret-key-for-jwt-signing", algorithm="HS256")
        request = AsyncMock()
        request.cookies = {SESSION_COOKIE: token}
        user = await get_dashboard_user(request)
        assert user is None
