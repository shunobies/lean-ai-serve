"""Tests for HashiCorp Vault key provider."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from lean_ai_serve.config import EncryptionAtRestConfig
from lean_ai_serve.security.vault import KEY_SIZE, VaultKeyProvider


@pytest.fixture
def vault_config():
    return EncryptionAtRestConfig(
        enabled=True,
        key_source="vault",
        vault_path="secret/data/lean-ai-serve/encryption-key",
        vault_key_field="key",
        vault_auth_method="token",
        vault_cache_ttl=300,
    )


@pytest.fixture
def test_key():
    return os.urandom(KEY_SIZE)


@pytest.fixture
def mock_hvac(test_key):
    """Create a mock hvac client with a pre-configured secret response."""
    mock_client = MagicMock()
    mock_client.secrets.kv.v2.read_secret_version.return_value = {
        "data": {"data": {"key": test_key.hex()}}
    }
    return mock_client


# ---------------------------------------------------------------------------
# Token auth
# ---------------------------------------------------------------------------


class TestTokenAuth:
    def test_fetch_key_with_token(self, vault_config, mock_hvac, test_key, monkeypatch):
        """Token auth fetches the key from Vault."""
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.setenv("VAULT_TOKEN", "s.test-token")

        provider = VaultKeyProvider(vault_config)
        provider._client = mock_hvac

        key = provider.fetch_key()
        assert key == test_key
        mock_hvac.secrets.kv.v2.read_secret_version.assert_called_once()

    def test_missing_vault_token(self, vault_config, monkeypatch):
        """Missing VAULT_TOKEN raises ValueError."""
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.delenv("VAULT_TOKEN", raising=False)

        mock_hvac_mod = MagicMock()
        provider = VaultKeyProvider(vault_config)
        with (
            patch.dict("sys.modules", {"hvac": mock_hvac_mod}),
            pytest.raises(ValueError, match="VAULT_TOKEN"),
        ):
            provider._get_client()


# ---------------------------------------------------------------------------
# AppRole auth
# ---------------------------------------------------------------------------


class TestAppRoleAuth:
    def test_approle_auth(self, monkeypatch, test_key):
        """AppRole auth authenticates and fetches the key."""
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.setenv("VAULT_ROLE_ID", "role-123")
        monkeypatch.setenv("VAULT_SECRET_ID", "secret-456")

        config = EncryptionAtRestConfig(
            enabled=True,
            key_source="vault",
            vault_auth_method="approle",
        )
        provider = VaultKeyProvider(config)

        mock_client = MagicMock()
        mock_client.auth.approle.login.return_value = {
            "auth": {"client_token": "s.app-token"}
        }
        mock_client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"key": test_key.hex()}}
        }

        with patch("lean_ai_serve.security.vault.hvac", create=True) as mock_hvac_mod:
            mock_hvac_mod.Client.return_value = mock_client
            provider._client = mock_client
            provider._authenticate()
            key = provider.fetch_key()

        assert key == test_key
        mock_client.auth.approle.login.assert_called_once_with(
            role_id="role-123", secret_id="secret-456"
        )

    def test_approle_missing_env_vars(self, monkeypatch):
        """Missing AppRole env vars raises ValueError."""
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.delenv("VAULT_ROLE_ID", raising=False)
        monkeypatch.delenv("VAULT_SECRET_ID", raising=False)

        config = EncryptionAtRestConfig(
            enabled=True,
            key_source="vault",
            vault_auth_method="approle",
        )
        provider = VaultKeyProvider(config)

        mock_client = MagicMock()
        provider._client = mock_client

        with pytest.raises(ValueError, match="VAULT_ROLE_ID"):
            provider._authenticate()


# ---------------------------------------------------------------------------
# Key caching
# ---------------------------------------------------------------------------


class TestKeyCaching:
    def test_cached_key_returned(self, vault_config, mock_hvac, test_key, monkeypatch):
        """Cached key is returned without hitting Vault again."""
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.setenv("VAULT_TOKEN", "s.test")

        provider = VaultKeyProvider(vault_config)
        provider._client = mock_hvac

        key1 = provider.fetch_key()
        key2 = provider.fetch_key()

        assert key1 == key2 == test_key
        # Should only call Vault once
        assert mock_hvac.secrets.kv.v2.read_secret_version.call_count == 1

    def test_cache_invalidation(self, vault_config, mock_hvac, test_key, monkeypatch):
        """invalidate_cache forces a re-fetch."""
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.setenv("VAULT_TOKEN", "s.test")

        provider = VaultKeyProvider(vault_config)
        provider._client = mock_hvac

        provider.fetch_key()
        provider.invalidate_cache()
        provider.fetch_key()

        assert mock_hvac.secrets.kv.v2.read_secret_version.call_count == 2

    def test_cache_ttl_expired(self, vault_config, mock_hvac, test_key, monkeypatch):
        """Expired cache triggers a re-fetch."""
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.setenv("VAULT_TOKEN", "s.test")

        provider = VaultKeyProvider(vault_config)
        provider._client = mock_hvac

        provider.fetch_key()
        # Simulate expiry
        provider._cache_time = 0.0
        provider.fetch_key()

        assert mock_hvac.secrets.kv.v2.read_secret_version.call_count == 2


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_retry_on_transient_error(self, vault_config, test_key, monkeypatch):
        """Transient error retries with backoff."""
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.setenv("VAULT_TOKEN", "s.test")

        mock_client = MagicMock()
        # Fail twice, succeed on third
        mock_client.secrets.kv.v2.read_secret_version.side_effect = [
            ConnectionError("timeout"),
            ConnectionError("timeout"),
            {"data": {"data": {"key": test_key.hex()}}},
        ]

        provider = VaultKeyProvider(vault_config)
        provider._client = mock_client

        with patch("lean_ai_serve.security.vault.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            mock_time.sleep = MagicMock()
            key = provider.fetch_key()

        assert key == test_key
        assert mock_client.secrets.kv.v2.read_secret_version.call_count == 3

    def test_all_retries_exhausted(self, vault_config, monkeypatch):
        """All retries exhausted raises RuntimeError."""
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.setenv("VAULT_TOKEN", "s.test")

        mock_client = MagicMock()
        mock_client.secrets.kv.v2.read_secret_version.side_effect = ConnectionError("down")

        provider = VaultKeyProvider(vault_config)
        provider._client = mock_client

        with patch("lean_ai_serve.security.vault.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            mock_time.sleep = MagicMock()
            with pytest.raises(RuntimeError, match="Failed to fetch key"):
                provider.fetch_key()

    def test_missing_vault_addr(self, vault_config, monkeypatch):
        """Missing VAULT_ADDR raises ValueError."""
        monkeypatch.delenv("VAULT_ADDR", raising=False)

        mock_hvac_mod = MagicMock()
        provider = VaultKeyProvider(vault_config)
        with (
            patch.dict("sys.modules", {"hvac": mock_hvac_mod}),
            pytest.raises(ValueError, match="VAULT_ADDR"),
        ):
            provider._get_client()

    def test_wrong_key_size(self, vault_config, monkeypatch):
        """Key with wrong size raises ValueError."""
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.setenv("VAULT_TOKEN", "s.test")

        mock_client = MagicMock()
        mock_client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"key": "abcd"}}  # Too short (2 bytes hex)
        }

        provider = VaultKeyProvider(vault_config)
        provider._client = mock_client

        with pytest.raises(RuntimeError):
            provider.fetch_key()

    def test_hvac_not_installed(self, vault_config, monkeypatch):
        """Missing hvac dependency raises ImportError."""
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")

        provider = VaultKeyProvider(vault_config)
        with (
            patch.dict("sys.modules", {"hvac": None}),
            pytest.raises(ImportError, match="hvac"),
        ):
            provider._get_client()

    def test_base64_key_format(self, vault_config, test_key, monkeypatch):
        """Key stored as base64 in Vault is decoded correctly."""
        import base64

        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.setenv("VAULT_TOKEN", "s.test")

        mock_client = MagicMock()
        mock_client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"key": base64.b64encode(test_key).decode()}}
        }

        provider = VaultKeyProvider(vault_config)
        provider._client = mock_client

        key = provider.fetch_key()
        assert key == test_key
