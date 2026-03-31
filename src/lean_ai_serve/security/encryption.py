"""AES-256-GCM encryption at rest for sensitive data."""

from __future__ import annotations

import base64
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from lean_ai_serve.config import EncryptionAtRestConfig

logger = logging.getLogger(__name__)

NONCE_SIZE = 12  # 96-bit nonce for AES-GCM
KEY_SIZE = 32  # 256-bit key


class EncryptionService:
    """Encrypts and decrypts data using AES-256-GCM.

    Key is loaded at initialization from file, environment variable, or vault.
    """

    def __init__(self, config: EncryptionAtRestConfig):
        self._key = self._load_key(config)
        self._aesgcm = AESGCM(self._key)
        logger.info("Encryption service initialized (key_source=%s)", config.key_source)

    @staticmethod
    def _load_key(config: EncryptionAtRestConfig) -> bytes:
        """Load the encryption key from the configured source."""
        if config.key_source == "file":
            if not config.key_file:
                raise ValueError("encryption.at_rest.key_file must be set when key_source='file'")
            key_path = os.path.expanduser(config.key_file)
            with open(key_path, "rb") as f:
                key = f.read()
            if len(key) != KEY_SIZE:
                raise ValueError(
                    f"Encryption key must be exactly {KEY_SIZE} bytes, got {len(key)}"
                )
            return key

        if config.key_source == "env":
            raw = os.environ.get(config.key_env_var, "")
            if not raw:
                raise ValueError(
                    f"Environment variable '{config.key_env_var}' not set or empty"
                )
            # Try hex decoding first, then base64
            try:
                key = bytes.fromhex(raw)
            except ValueError:
                key = base64.b64decode(raw)
            if len(key) != KEY_SIZE:
                raise ValueError(
                    f"Encryption key must be exactly {KEY_SIZE} bytes, got {len(key)}"
                )
            return key

        if config.key_source == "vault":
            raise NotImplementedError(
                "HashiCorp Vault integration not yet implemented. "
                "Use key_source='file' or key_source='env' for now."
            )

        raise ValueError(f"Unknown key_source: {config.key_source}")

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string. Returns base64-encoded nonce + ciphertext."""
        nonce = os.urandom(NONCE_SIZE)
        ct = self._aesgcm.encrypt(nonce, plaintext.encode(), None)
        return base64.b64encode(nonce + ct).decode()

    def decrypt(self, ciphertext_b64: str) -> str:
        """Decrypt a base64-encoded ciphertext. Returns the original string."""
        raw = base64.b64decode(ciphertext_b64)
        if len(raw) <= NONCE_SIZE:
            raise ValueError("Ciphertext too short")
        nonce = raw[:NONCE_SIZE]
        ct = raw[NONCE_SIZE:]
        return self._aesgcm.decrypt(nonce, ct, None).decode()


def generate_key_file(path: str) -> None:
    """Generate a random 256-bit key and write it to a file.

    Utility for initial setup::

        python -c "from lean_ai_serve.security.encryption import \\
            generate_key_file; generate_key_file('key.bin')"
    """
    key = os.urandom(KEY_SIZE)
    path = os.path.expanduser(path)
    with open(path, "wb") as f:
        f.write(key)
    os.chmod(path, 0o600)
    logger.info("Generated encryption key at %s", path)
