"""Tests for LDAP authentication service (mocked ldap3)."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lean_ai_serve.config import LDAPConfig


@pytest.fixture
def ldap_config() -> LDAPConfig:
    return LDAPConfig(
        server_url="ldap://test-server:389",
        bind_dn="cn=service,dc=example,dc=com",
        bind_password_env="TEST_LDAP_BIND_PW",
        user_search_base="ou=users,dc=example,dc=com",
        user_search_filter="(sAMAccountName={username})",
        group_search_base="ou=groups,dc=example,dc=com",
        group_role_mapping={
            "cn=admins,ou=groups,dc=example,dc=com": "admin",
            "cn=ml-team,ou=groups,dc=example,dc=com": "user",
        },
        default_role="user",
        cache_ttl=60,
        connection_pool_size=2,
    )


@pytest.fixture
def mock_ldap3():
    """Mock the ldap3 module for testing without a real LDAP server."""
    with patch.dict("sys.modules", {}):
        mock = MagicMock()
        mock.ALL = "ALL"
        mock.SUBTREE = "SUBTREE"

        # Mock entry
        mock_entry = MagicMock()
        mock_entry.entry_dn = "cn=testuser,ou=users,dc=example,dc=com"
        mock_entry.displayName.value = "Test User"
        mock_entry.cn.value = "testuser"

        # Mock connection
        mock_conn = MagicMock()
        mock_conn.bound = True
        mock_conn.entries = [mock_entry]

        def mock_search(**kwargs):
            mock_conn.entries = [mock_entry]

        mock_conn.search = mock_search
        mock.Connection.return_value = mock_conn
        mock.Server.return_value = MagicMock()

        yield mock, mock_conn, mock_entry


async def test_authenticate_success(ldap_config, mock_ldap3):
    """Successful LDAP authentication should return AuthUser with mapped roles."""
    mock, mock_conn, mock_entry = mock_ldap3
    os.environ["TEST_LDAP_BIND_PW"] = "service-password"

    try:
        with patch.dict("sys.modules", {"ldap3": mock, "ldap3.core.exceptions": MagicMock()}):
            # Re-import to pick up mocked ldap3
            import importlib

            import lean_ai_serve.security.ldap_auth as ldap_mod

            importlib.reload(ldap_mod)

            service = ldap_mod.LDAPService(ldap_config)

            # Mock the internal methods
            service._find_user_dn = AsyncMock(
                return_value="cn=testuser,ou=users,dc=example,dc=com"
            )
            service._verify_password = AsyncMock(return_value=True)
            service._get_user_groups = AsyncMock(
                return_value=["cn=ml-team,ou=groups,dc=example,dc=com"]
            )
            service._get_display_name = AsyncMock(return_value="Test User")

            # Mock pool
            service._pool = MagicMock()
            service._pool.get = AsyncMock(return_value=mock_conn)
            service._pool.put = AsyncMock()

            user = await service.authenticate("testuser", "password123")
            assert user is not None
            assert user.user_id == "testuser"
            assert user.display_name == "Test User"
            assert "user" in user.roles
            assert user.auth_method == "ldap"
    finally:
        del os.environ["TEST_LDAP_BIND_PW"]


async def test_authenticate_bad_password(ldap_config, mock_ldap3):
    """Failed password bind should return None."""
    mock, mock_conn, _ = mock_ldap3
    os.environ["TEST_LDAP_BIND_PW"] = "service-password"

    try:
        with patch.dict("sys.modules", {"ldap3": mock, "ldap3.core.exceptions": MagicMock()}):
            import importlib

            import lean_ai_serve.security.ldap_auth as ldap_mod

            importlib.reload(ldap_mod)

            service = ldap_mod.LDAPService(ldap_config)
            service._find_user_dn = AsyncMock(
                return_value="cn=testuser,ou=users,dc=example,dc=com"
            )
            service._verify_password = AsyncMock(return_value=False)

            service._pool = MagicMock()
            service._pool.get = AsyncMock(return_value=mock_conn)
            service._pool.put = AsyncMock()

            user = await service.authenticate("testuser", "wrong")
            assert user is None
    finally:
        del os.environ["TEST_LDAP_BIND_PW"]


async def test_authenticate_user_not_found(ldap_config, mock_ldap3):
    """Non-existent user should return None."""
    mock, mock_conn, _ = mock_ldap3
    os.environ["TEST_LDAP_BIND_PW"] = "service-password"

    try:
        with patch.dict("sys.modules", {"ldap3": mock, "ldap3.core.exceptions": MagicMock()}):
            import importlib

            import lean_ai_serve.security.ldap_auth as ldap_mod

            importlib.reload(ldap_mod)

            service = ldap_mod.LDAPService(ldap_config)
            service._find_user_dn = AsyncMock(return_value=None)

            service._pool = MagicMock()
            service._pool.get = AsyncMock(return_value=mock_conn)
            service._pool.put = AsyncMock()

            user = await service.authenticate("nonexistent", "password")
            assert user is None
    finally:
        del os.environ["TEST_LDAP_BIND_PW"]


async def test_cache_hit(ldap_config, mock_ldap3):
    """Second call with same credentials should use cache."""
    mock, mock_conn, _ = mock_ldap3
    os.environ["TEST_LDAP_BIND_PW"] = "service-password"

    try:
        with patch.dict("sys.modules", {"ldap3": mock, "ldap3.core.exceptions": MagicMock()}):
            import importlib

            import lean_ai_serve.security.ldap_auth as ldap_mod

            importlib.reload(ldap_mod)

            service = ldap_mod.LDAPService(ldap_config)
            service._find_user_dn = AsyncMock(
                return_value="cn=testuser,ou=users,dc=example,dc=com"
            )
            service._verify_password = AsyncMock(return_value=True)
            service._get_user_groups = AsyncMock(return_value=[])
            service._get_display_name = AsyncMock(return_value="Test User")

            service._pool = MagicMock()
            service._pool.get = AsyncMock(return_value=mock_conn)
            service._pool.put = AsyncMock()

            # First call — hits LDAP
            user1 = await service.authenticate("testuser", "password123")
            assert user1 is not None

            # Second call — should use cache (verify_password not called again)
            service._verify_password.reset_mock()
            user2 = await service.authenticate("testuser", "password123")
            assert user2 is not None
            assert user2.user_id == user1.user_id
            service._verify_password.assert_not_called()
    finally:
        del os.environ["TEST_LDAP_BIND_PW"]


def test_group_role_mapping(ldap_config, mock_ldap3):
    """Group-to-role mapping should be case-insensitive."""
    mock, mock_conn, _ = mock_ldap3
    os.environ["TEST_LDAP_BIND_PW"] = "service-password"

    try:
        with patch.dict("sys.modules", {"ldap3": mock, "ldap3.core.exceptions": MagicMock()}):
            import importlib

            import lean_ai_serve.security.ldap_auth as ldap_mod

            importlib.reload(ldap_mod)

            service = ldap_mod.LDAPService(ldap_config)
            # Upper-case DN should match lower-case config
            roles = service._map_groups_to_roles(
                ["CN=Admins,OU=Groups,DC=Example,DC=Com"]
            )
            assert "admin" in roles
    finally:
        del os.environ["TEST_LDAP_BIND_PW"]


def test_default_role(ldap_config, mock_ldap3):
    """No matching groups should fall back to default role."""
    mock, mock_conn, _ = mock_ldap3
    os.environ["TEST_LDAP_BIND_PW"] = "service-password"

    try:
        with patch.dict("sys.modules", {"ldap3": mock, "ldap3.core.exceptions": MagicMock()}):
            import importlib

            import lean_ai_serve.security.ldap_auth as ldap_mod

            importlib.reload(ldap_mod)

            service = ldap_mod.LDAPService(ldap_config)
            roles = service._map_groups_to_roles(
                ["cn=unknown,ou=groups,dc=example,dc=com"]
            )
            assert roles == ["user"]  # default_role
    finally:
        del os.environ["TEST_LDAP_BIND_PW"]


def test_missing_bind_password(ldap_config, mock_ldap3):
    """Missing bind password env var should raise ValueError."""
    mock, _, _ = mock_ldap3
    # Ensure the env var is not set
    os.environ.pop("TEST_LDAP_BIND_PW", None)

    with patch.dict("sys.modules", {"ldap3": mock, "ldap3.core.exceptions": MagicMock()}):
        import importlib

        import lean_ai_serve.security.ldap_auth as ldap_mod

        importlib.reload(ldap_mod)

        with pytest.raises(ValueError, match="not found"):
            ldap_mod.LDAPService(ldap_config)
