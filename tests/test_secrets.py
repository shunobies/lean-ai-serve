"""Tests for config secret resolution (ENV[] and ENC[] patterns)."""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag

from lean_ai_serve.security.secrets import (
    _KEY_SIZE,
    _has_encrypted_values,
    decrypt_value,
    encrypt_value,
    load_key_from_file,
    load_master_key,
    resolve_config_secrets,
)


@pytest.fixture
def master_key() -> bytes:
    """Generate a random 256-bit key for testing."""
    return os.urandom(_KEY_SIZE)


@pytest.fixture
def key_file(tmp_path, master_key) -> str:
    """Write master key to a temp file."""
    path = tmp_path / "master.key"
    path.write_bytes(master_key)
    return str(path)


# ---------------------------------------------------------------------------
# Encrypt / decrypt round-trip
# ---------------------------------------------------------------------------


class TestEncryptDecrypt:
    def test_round_trip(self, master_key):
        """Encrypt then decrypt returns original value."""
        original = "my-super-secret"
        encrypted = encrypt_value(original, master_key)
        assert encrypted.startswith("ENC[")
        assert encrypted.endswith("]")
        decrypted = decrypt_value(encrypted, master_key)
        assert decrypted == original

    def test_different_nonces(self, master_key):
        """Each encryption produces a different ciphertext (random nonce)."""
        value = "same-input"
        enc1 = encrypt_value(value, master_key)
        enc2 = encrypt_value(value, master_key)
        assert enc1 != enc2  # Different nonces
        assert decrypt_value(enc1, master_key) == value
        assert decrypt_value(enc2, master_key) == value

    def test_wrong_key_fails(self, master_key):
        """Decryption with wrong key raises error."""
        encrypted = encrypt_value("secret", master_key)
        wrong_key = os.urandom(_KEY_SIZE)
        with pytest.raises(InvalidTag):
            decrypt_value(encrypted, wrong_key)

    def test_empty_string(self, master_key):
        """Empty string can be encrypted and decrypted."""
        encrypted = encrypt_value("", master_key)
        assert decrypt_value(encrypted, master_key) == ""

    def test_unicode(self, master_key):
        """Unicode values can be encrypted and decrypted."""
        original = "пароль-密码-パスワード"
        encrypted = encrypt_value(original, master_key)
        assert decrypt_value(encrypted, master_key) == original

    def test_decrypt_raw_base64(self, master_key):
        """decrypt_value accepts raw base64 without ENC[] wrapper."""
        encrypted = encrypt_value("test", master_key)
        raw = encrypted[4:-1]  # Strip "ENC[" and "]"
        assert decrypt_value(raw, master_key) == "test"

    def test_corrupted_ciphertext(self, master_key):
        """Corrupted (too short) ciphertext raises ValueError."""
        with pytest.raises(ValueError, match="too short"):
            decrypt_value("ENC[AAAA]", master_key)


# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------


class TestKeyLoading:
    def test_load_from_file(self, key_file, master_key):
        """load_key_from_file reads the correct key."""
        loaded = load_key_from_file(key_file)
        assert loaded == master_key

    def test_load_from_file_wrong_size(self, tmp_path):
        """Key file with wrong size raises ValueError."""
        bad_key = tmp_path / "bad.key"
        bad_key.write_bytes(b"too-short")
        with pytest.raises(ValueError, match="32 bytes"):
            load_key_from_file(str(bad_key))

    def test_load_master_key_file_source(self, key_file, master_key):
        """load_master_key with file source."""
        config = {"at_rest": {"key_source": "file", "key_file": key_file}}
        assert load_master_key(config) == master_key

    def test_load_master_key_env_source_hex(self, monkeypatch, master_key):
        """load_master_key with env source (hex encoding)."""
        monkeypatch.setenv("TEST_ENC_KEY", master_key.hex())
        config = {"at_rest": {"key_source": "env", "key_env_var": "TEST_ENC_KEY"}}
        assert load_master_key(config) == master_key

    def test_load_master_key_env_source_b64(self, monkeypatch, master_key):
        """load_master_key with env source (base64 encoding)."""
        import base64

        monkeypatch.setenv("TEST_ENC_KEY", base64.b64encode(master_key).decode())
        config = {"at_rest": {"key_source": "env", "key_env_var": "TEST_ENC_KEY"}}
        assert load_master_key(config) == master_key

    def test_load_master_key_missing_env(self, monkeypatch):
        """load_master_key with missing env var raises ValueError."""
        monkeypatch.delenv("NONEXISTENT_KEY", raising=False)
        config = {"at_rest": {"key_source": "env", "key_env_var": "NONEXISTENT_KEY"}}
        with pytest.raises(ValueError, match="NONEXISTENT_KEY"):
            load_master_key(config)

    def test_load_master_key_vault_requires_hvac(self):
        """Vault source raises ImportError when hvac is not installed."""
        config = {"at_rest": {"key_source": "vault"}}
        with pytest.raises(ImportError, match="hvac"):
            load_master_key(config)

    def test_load_master_key_flat_dict(self, key_file, master_key):
        """load_master_key accepts flat dict (no at_rest wrapper)."""
        config = {"key_source": "file", "key_file": key_file}
        assert load_master_key(config) == master_key


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


class TestResolveConfigSecrets:
    def test_env_resolution(self, monkeypatch):
        """ENV[VAR_NAME] is resolved from the environment."""
        monkeypatch.setenv("MY_SECRET", "resolved-value")
        data = {"security": {"jwt_secret": "ENV[MY_SECRET]"}}
        result = resolve_config_secrets(data)
        assert result["security"]["jwt_secret"] == "resolved-value"

    def test_env_missing_raises(self, monkeypatch):
        """ENV[] with missing env var raises ValueError."""
        monkeypatch.delenv("MISSING_VAR", raising=False)
        data = {"cache": {"token": "ENV[MISSING_VAR]"}}
        with pytest.raises(ValueError, match="MISSING_VAR"):
            resolve_config_secrets(data)

    def test_enc_resolution(self, master_key, key_file):
        """ENC[...] values are decrypted."""
        encrypted = encrypt_value("my-jwt-secret", master_key)
        data = {
            "security": {"jwt_secret": encrypted},
            "encryption": {"at_rest": {"key_source": "file", "key_file": key_file}},
        }
        result = resolve_config_secrets(data, data["encryption"])
        assert result["security"]["jwt_secret"] == "my-jwt-secret"

    def test_encryption_section_not_processed(self, master_key, key_file):
        """The encryption section itself is passed through unmodified."""
        encrypted = encrypt_value("test", master_key)
        data = {
            "security": {"jwt_secret": encrypted},
            "encryption": {"at_rest": {"key_source": "file", "key_file": key_file}},
        }
        result = resolve_config_secrets(data, data["encryption"])
        assert result["encryption"]["at_rest"]["key_file"] == key_file

    def test_mixed_env_and_enc(self, monkeypatch, master_key, key_file):
        """Both ENV[] and ENC[] work in the same config."""
        monkeypatch.setenv("HF_TOKEN", "hf_abc123")
        encrypted_jwt = encrypt_value("jwt-secret", master_key)
        data = {
            "security": {"jwt_secret": encrypted_jwt},
            "cache": {"huggingface_token": "ENV[HF_TOKEN]"},
            "encryption": {"at_rest": {"key_source": "file", "key_file": key_file}},
        }
        result = resolve_config_secrets(data, data["encryption"])
        assert result["security"]["jwt_secret"] == "jwt-secret"
        assert result["cache"]["huggingface_token"] == "hf_abc123"

    def test_plain_values_unchanged(self):
        """Plain string values pass through unchanged."""
        data = {"server": {"host": "0.0.0.0", "port": 8420}}
        result = resolve_config_secrets(data)
        assert result == data

    def test_nested_list_resolution(self, monkeypatch):
        """ENV[] works inside lists."""
        monkeypatch.setenv("PATTERN_NAME", "SSN")
        data = {
            "security": {
                "content_filtering": {
                    "patterns": [{"name": "ENV[PATTERN_NAME]", "action": "warn"}]
                }
            }
        }
        result = resolve_config_secrets(data)
        assert result["security"]["content_filtering"]["patterns"][0]["name"] == "SSN"

    def test_enc_without_encryption_config_raises(self):
        """ENC[] values without encryption config raises ValueError."""
        data = {"security": {"jwt_secret": "ENC[somebase64data]"}}
        with pytest.raises(ValueError, match="no encryption section"):
            resolve_config_secrets(data)

    def test_no_secrets_no_key_needed(self):
        """Config without ENV[]/ENC[] doesn't require encryption config."""
        data = {"server": {"port": 8420}, "security": {"mode": "api_key"}}
        result = resolve_config_secrets(data)
        assert result["server"]["port"] == 8420

    def test_non_string_types_preserved(self):
        """Non-string types (int, bool, None) are preserved."""
        data = {
            "server": {"port": 8420},
            "metrics": {"enabled": True},
            "optional": None,
        }
        result = resolve_config_secrets(data)
        assert result["server"]["port"] == 8420
        assert result["metrics"]["enabled"] is True
        assert result["optional"] is None


# ---------------------------------------------------------------------------
# _has_encrypted_values helper
# ---------------------------------------------------------------------------


class TestHasEncryptedValues:
    def test_finds_enc_in_nested_dict(self):
        assert _has_encrypted_values({"a": {"b": "ENC[data]"}}) is True

    def test_finds_enc_in_list(self):
        assert _has_encrypted_values({"a": ["ENC[data]"]}) is True

    def test_no_enc_returns_false(self):
        assert _has_encrypted_values({"a": "plain", "b": 42}) is False

    def test_env_not_counted_as_enc(self):
        assert _has_encrypted_values({"a": "ENV[VAR]"}) is False

    def test_partial_prefix_not_matched(self):
        assert _has_encrypted_values({"a": "ENC_not_bracket"}) is False
