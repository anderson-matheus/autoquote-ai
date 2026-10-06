"""At-rest protection for the few identifiers we must keep (contact address, CEP)."""

from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken


class FieldCipher:
    """Symmetric encryption (Fernet: AES-128-CBC + HMAC-SHA256) for single columns."""

    def __init__(self, key: str | bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def ephemeral(cls) -> FieldCipher:
        """Random key for tests: data cannot be decrypted after a restart."""
        return cls(Fernet.generate_key())

    @classmethod
    def from_passphrase(cls, passphrase: str) -> FieldCipher:
        """Deterministic key for local development only (production requires a real key)."""
        digest = hashlib.sha256(f"autoquote-dev-key:{passphrase}".encode()).digest()
        return cls(base64.urlsafe_b64encode(digest))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("cannot decrypt field: wrong key or tampered value") from exc


def contact_hash(address: str, secret: str) -> str:
    """Stable pseudonymous id for a contact (lookup key that is not the phone number)."""
    return hmac.new(secret.encode(), address.encode(), hashlib.sha256).hexdigest()
