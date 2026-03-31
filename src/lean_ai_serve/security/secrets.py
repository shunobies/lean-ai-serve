"""Config secret resolution — ENV[] and ENC[] value handling.

Provides two patterns for managing secrets in YAML configuration:

- ``ENV[VAR_NAME]`` — resolved from the named environment variable at load time.
- ``ENC[ciphertext]`` — decrypted using the master key from ``encryption.at_rest``.

The master key is the same AES-256-GCM key used by ``EncryptionService`` for
data-at-rest encryption.  A single key protects both config secrets and
runtime-encrypted audit data.

Usage in config.yaml::

    security:
      jwt_secret: "ENC[base64-ciphertext]"  # encrypted with master key
    cache:
      huggingface_token: "ENV[HF_TOKEN]"    # read from env var

Generate a master key::

    lean-ai-serve config generate-key /path/to/master.key

Encrypt a value for pasting into YAML::

    lean-ai-serve config encrypt-value "my-secret" --key-file /path/to/master.key
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_NONCE_SIZE = 12  # 96-bit nonce for AES-GCM
_KEY_SIZE = 32  # 256-bit key

_ENV_PREFIX = "ENV["
_ENC_PREFIX = "ENC["
_SUFFIX = "]"


# ---------------------------------------------------------------------------
# Key loading (mirrors EncryptionService._load_key but works from raw dict)
# ---------------------------------------------------------------------------


def load_master_key(encryption_config: dict) -> bytes:
    """Load the master key from an encryption config dict.

    Accepts either the full ``encryption`` section (with ``at_rest`` nested)
    or the ``at_rest`` subsection directly.
    """
    at_rest = encryption_config.get("at_rest", encryption_config)
    key_source = at_rest.get("key_source", "file")

    if key_source == "file":
        key_file = at_rest.get("key_file", "")
        if not key_file:
            raise ValueError(
                "encryption.at_rest.key_file required to decrypt ENC[] config values"
            )
        key_path = os.path.expanduser(key_file)
        with open(key_path, "rb") as f:
            key = f.read()
        if len(key) != _KEY_SIZE:
            raise ValueError(f"Master key must be {_KEY_SIZE} bytes, got {len(key)}")
        return key

    if key_source == "env":
        key_env = at_rest.get("key_env_var", "LEAN_AI_ENCRYPTION_KEY")
        raw = os.environ.get(key_env, "")
        if not raw:
            raise ValueError(
                f"Environment variable '{key_env}' required to decrypt ENC[] config values"
            )
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            key = base64.b64decode(raw)
        if len(key) != _KEY_SIZE:
            raise ValueError(f"Master key must be {_KEY_SIZE} bytes, got {len(key)}")
        return key

    if key_source == "vault":
        from lean_ai_serve.config import EncryptionAtRestConfig
        from lean_ai_serve.security.vault import VaultKeyProvider

        vault_cfg = EncryptionAtRestConfig(**at_rest)
        provider = VaultKeyProvider(vault_cfg)
        return provider.fetch_key()

    raise ValueError(f"Unknown key_source: {key_source}")


def load_key_from_file(path: str) -> bytes:
    """Load a master key directly from a file path."""
    key_path = os.path.expanduser(path)
    with open(key_path, "rb") as f:
        key = f.read()
    if len(key) != _KEY_SIZE:
        raise ValueError(f"Master key must be {_KEY_SIZE} bytes, got {len(key)}")
    return key


# ---------------------------------------------------------------------------
# Encrypt / decrypt
# ---------------------------------------------------------------------------


def encrypt_value(plaintext: str, key: bytes) -> str:
    """Encrypt a value for use in config YAML.

    Returns a string in ``ENC[...]`` format ready to paste into config.yaml.
    Uses AES-256-GCM — same algorithm as ``EncryptionService``.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(_NONCE_SIZE)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    b64 = base64.b64encode(nonce + ct).decode()
    return f"ENC[{b64}]"


def decrypt_value(enc_string: str, key: bytes) -> str:
    """Decrypt an ``ENC[...]`` config value.

    Accepts either the full ``ENC[...]`` wrapper or the raw base64 ciphertext.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    # Strip ENC[] wrapper if present
    if enc_string.startswith(_ENC_PREFIX) and enc_string.endswith(_SUFFIX):
        b64 = enc_string[len(_ENC_PREFIX) : -len(_SUFFIX)]
    else:
        b64 = enc_string

    raw = base64.b64decode(b64)
    if len(raw) <= _NONCE_SIZE:
        raise ValueError("Encrypted value too short — corrupted ciphertext?")
    nonce = raw[:_NONCE_SIZE]
    ct = raw[_NONCE_SIZE:]
    return AESGCM(key).decrypt(nonce, ct, None).decode()


# ---------------------------------------------------------------------------
# Config dict resolution
# ---------------------------------------------------------------------------


def resolve_config_secrets(
    data: dict[str, Any],
    encryption_config: dict | None = None,
) -> dict[str, Any]:
    """Resolve ``ENV[]`` and ``ENC[]`` references in a config dictionary.

    Walks the dict recursively.  The ``encryption`` key itself is skipped
    (it's the bootstrap for the master key and cannot contain encrypted values).

    The master key is loaded lazily — only when ``ENC[]`` values are actually
    found in the config.
    """
    key: bytes | None = None
    if _has_encrypted_values(data):
        if encryption_config is None:
            raise ValueError(
                "ENC[] values found in config but no encryption section defined. "
                "Add encryption.at_rest with key_file or key_env_var."
            )
        key = load_master_key(encryption_config)

    return _resolve_dict(data, key, skip_keys={"encryption"})


def _has_encrypted_values(data: Any) -> bool:
    """Check recursively whether any string contains an ENC[] reference."""
    if isinstance(data, str):
        return data.startswith(_ENC_PREFIX) and data.endswith(_SUFFIX)
    if isinstance(data, dict):
        return any(_has_encrypted_values(v) for v in data.values())
    if isinstance(data, list):
        return any(_has_encrypted_values(v) for v in data)
    return False


def _resolve_value(value: str, key: bytes | None) -> str:
    """Resolve a single string value — handle ENV[] and ENC[] prefixes."""
    if value.startswith(_ENV_PREFIX) and value.endswith(_SUFFIX):
        var_name = value[len(_ENV_PREFIX) : -len(_SUFFIX)]
        resolved = os.environ.get(var_name)
        if resolved is None:
            raise ValueError(
                f"Environment variable '{var_name}' not set "
                f"(referenced as ENV[{var_name}] in config)"
            )
        return resolved

    if value.startswith(_ENC_PREFIX) and value.endswith(_SUFFIX):
        if key is None:
            raise ValueError(
                "ENC[] value found but no master key available. "
                "Configure encryption.at_rest.key_file or key_env_var."
            )
        return decrypt_value(value, key)

    return value


def _resolve_dict(
    data: dict[str, Any],
    key: bytes | None,
    skip_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Recursively resolve string values in a dict."""
    result: dict[str, Any] = {}
    for k, v in data.items():
        if skip_keys and k in skip_keys:
            result[k] = v
            continue
        result[k] = _resolve_any(v, key)
    return result


def _resolve_any(value: Any, key: bytes | None) -> Any:
    """Resolve a value of any type."""
    if isinstance(value, str):
        return _resolve_value(value, key)
    if isinstance(value, dict):
        return _resolve_dict(value, key)
    if isinstance(value, list):
        return [_resolve_any(v, key) for v in value]
    return value
