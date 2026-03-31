"""HashiCorp Vault integration for encryption key management.

Supports two authentication methods:

- **Token auth**: uses ``VAULT_TOKEN`` environment variable (default).
- **AppRole auth**: uses ``VAULT_ROLE_ID`` + ``VAULT_SECRET_ID`` env vars.

The Vault address is read from ``VAULT_ADDR`` (standard Vault env var).

Key is fetched from a configurable secret path and cached in memory with a
TTL to avoid hitting Vault on every request.  The ``hvac`` library is an
optional dependency (``pip install lean-ai-serve[vault]``).
"""

from __future__ import annotations

import base64
import logging
import os
import time

from lean_ai_serve.config import EncryptionAtRestConfig

logger = logging.getLogger(__name__)

KEY_SIZE = 32  # 256-bit key
_MAX_RETRIES = 3
_RETRY_BACKOFF = 1.0  # Seconds, doubled on each retry


class VaultKeyProvider:
    """Fetches and caches encryption keys from HashiCorp Vault."""

    def __init__(self, config: EncryptionAtRestConfig):
        self._config = config
        self._cached_key: bytes | None = None
        self._cache_time: float = 0.0
        self._client = None

    def _get_client(self):
        """Create or return the Vault client (lazy init)."""
        if self._client is not None:
            return self._client

        try:
            import hvac
        except ImportError as exc:
            raise ImportError(
                "hvac is required for Vault integration. "
                "Install with: pip install lean-ai-serve[vault]"
            ) from exc

        vault_addr = os.environ.get("VAULT_ADDR", "")
        if not vault_addr:
            raise ValueError("VAULT_ADDR environment variable must be set for Vault key source")

        self._client = hvac.Client(url=vault_addr)
        self._authenticate()
        return self._client

    def _authenticate(self) -> None:
        """Authenticate to Vault using the configured method."""
        config = self._config

        if config.vault_auth_method == "token":
            token = os.environ.get("VAULT_TOKEN", "")
            if not token:
                raise ValueError("VAULT_TOKEN must be set for token auth")
            self._client.token = token

        elif config.vault_auth_method == "approle":
            role_id = os.environ.get(config.vault_role_id_env, "")
            secret_id = os.environ.get(config.vault_secret_id_env, "")
            if not role_id or not secret_id:
                raise ValueError(
                    f"Both {config.vault_role_id_env} and {config.vault_secret_id_env} "
                    f"must be set for AppRole auth"
                )
            resp = self._client.auth.approle.login(
                role_id=role_id, secret_id=secret_id
            )
            self._client.token = resp["auth"]["client_token"]
            logger.info("Vault AppRole authentication successful")

        else:
            raise ValueError(f"Unknown vault_auth_method: {config.vault_auth_method}")

    def fetch_key(self) -> bytes:
        """Fetch the encryption key from Vault (with caching).

        Returns the raw 32-byte key.  Retries with exponential backoff on
        transient errors.
        """
        # Check cache
        cache_age = time.monotonic() - self._cache_time
        if self._cached_key and cache_age < self._config.vault_cache_ttl:
            return self._cached_key

        client = self._get_client()
        last_error: Exception | None = None
        backoff = _RETRY_BACKOFF

        for attempt in range(_MAX_RETRIES):
            try:
                secret = client.secrets.kv.v2.read_secret_version(
                    path=self._config.vault_path.removeprefix("secret/data/"),
                    raise_on_deleted_version=True,
                )
                raw_value = secret["data"]["data"][self._config.vault_key_field]
                key = self._decode_key(raw_value)

                # Cache the result
                self._cached_key = key
                self._cache_time = time.monotonic()
                logger.info("Encryption key fetched from Vault (path=%s)", self._config.vault_path)
                return key

            except Exception as exc:
                last_error = exc
                if attempt < _MAX_RETRIES - 1:
                    logger.warning(
                        "Vault fetch attempt %d failed: %s — retrying in %.1fs",
                        attempt + 1, exc, backoff,
                    )
                    time.sleep(backoff)
                    backoff *= 2

        raise RuntimeError(
            f"Failed to fetch key from Vault after {_MAX_RETRIES} attempts"
        ) from last_error

    @staticmethod
    def _decode_key(raw: str) -> bytes:
        """Decode a key value from Vault (hex or base64)."""
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            key = base64.b64decode(raw)

        if len(key) != KEY_SIZE:
            raise ValueError(
                f"Vault key must be exactly {KEY_SIZE} bytes, got {len(key)}"
            )
        return key

    def invalidate_cache(self) -> None:
        """Force a re-fetch on the next ``fetch_key`` call."""
        self._cached_key = None
        self._cache_time = 0.0
