"""Tests for AES-256-GCM encryption at rest."""

from __future__ import annotations

import os

import pytest

from lean_ai_serve.config import EncryptionAtRestConfig
from lean_ai_serve.security.encryption import KEY_SIZE, EncryptionService, generate_key_file


@pytest.fixture
def key_file(tmp_path) -> str:
    """Create a valid encryption key file."""
    path = str(tmp_path / "test.key")
    key = os.urandom(KEY_SIZE)
    with open(path, "wb") as f:
        f.write(key)
    return path


@pytest.fixture
def encryption(key_file) -> EncryptionService:
    """Create an EncryptionService with a file-based key."""
    config = EncryptionAtRestConfig(
        enabled=True,
        key_source="file",
        key_file=key_file,
    )
    return EncryptionService(config)


def test_encrypt_decrypt_roundtrip(encryption: EncryptionService):
    """Encrypting then decrypting should return the original text."""
    original = "This is sensitive PHI data: SSN 123-45-6789"
    encrypted = encryption.encrypt(original)
    assert encrypted != original
    decrypted = encryption.decrypt(encrypted)
    assert decrypted == original


def test_different_ciphertexts(encryption: EncryptionService):
    """Encrypting the same plaintext twice should produce different ciphertexts (random nonce)."""
    plaintext = "Same text"
    ct1 = encryption.encrypt(plaintext)
    ct2 = encryption.encrypt(plaintext)
    assert ct1 != ct2
    # Both should decrypt to the same value
    assert encryption.decrypt(ct1) == plaintext
    assert encryption.decrypt(ct2) == plaintext


def test_tamper_detection(encryption: EncryptionService):
    """Modifying ciphertext should cause decryption to fail."""
    encrypted = encryption.encrypt("secret")
    # Tamper with the ciphertext
    import base64

    raw = bytearray(base64.b64decode(encrypted))
    raw[-1] ^= 0xFF  # Flip bits in the last byte
    tampered = base64.b64encode(bytes(raw)).decode()
    from cryptography.exceptions import InvalidTag

    with pytest.raises(InvalidTag):
        encryption.decrypt(tampered)


def test_key_from_env(tmp_path):
    """Should load key from an environment variable (hex-encoded)."""
    key = os.urandom(KEY_SIZE)
    os.environ["TEST_ENCRYPTION_KEY"] = key.hex()
    try:
        config = EncryptionAtRestConfig(
            enabled=True,
            key_source="env",
            key_env_var="TEST_ENCRYPTION_KEY",
        )
        svc = EncryptionService(config)
        ct = svc.encrypt("hello")
        assert svc.decrypt(ct) == "hello"
    finally:
        del os.environ["TEST_ENCRYPTION_KEY"]


def test_key_from_env_base64(tmp_path):
    """Should load key from an environment variable (base64-encoded)."""
    import base64

    key = os.urandom(KEY_SIZE)
    os.environ["TEST_ENCRYPTION_KEY_B64"] = base64.b64encode(key).decode()
    try:
        config = EncryptionAtRestConfig(
            enabled=True,
            key_source="env",
            key_env_var="TEST_ENCRYPTION_KEY_B64",
        )
        svc = EncryptionService(config)
        ct = svc.encrypt("hello")
        assert svc.decrypt(ct) == "hello"
    finally:
        del os.environ["TEST_ENCRYPTION_KEY_B64"]


def test_invalid_key_size(tmp_path):
    """Key file with wrong size should raise ValueError."""
    path = str(tmp_path / "bad.key")
    with open(path, "wb") as f:
        f.write(os.urandom(16))  # 16 bytes instead of 32
    config = EncryptionAtRestConfig(
        enabled=True, key_source="file", key_file=path
    )
    with pytest.raises(ValueError, match="exactly"):
        EncryptionService(config)


def test_generate_key_file(tmp_path):
    """generate_key_file should create a valid key."""
    path = str(tmp_path / "generated.key")
    generate_key_file(path)
    with open(path, "rb") as f:
        key = f.read()
    assert len(key) == KEY_SIZE

    # Should work with EncryptionService
    config = EncryptionAtRestConfig(
        enabled=True, key_source="file", key_file=path
    )
    svc = EncryptionService(config)
    assert svc.decrypt(svc.encrypt("test")) == "test"


def test_vault_not_implemented():
    """Vault key source should raise NotImplementedError."""
    config = EncryptionAtRestConfig(
        enabled=True, key_source="vault"
    )
    with pytest.raises(NotImplementedError):
        EncryptionService(config)


def test_short_ciphertext(encryption: EncryptionService):
    """Ciphertext too short should raise ValueError."""
    import base64

    short = base64.b64encode(b"short").decode()
    with pytest.raises(ValueError, match="too short"):
        encryption.decrypt(short)
