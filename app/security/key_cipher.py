import base64
import binascii
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES = 12
_AES_CONTEXT = b"rhythm-llm-key-aes-256-gcm-v1"
_HMAC_CONTEXT = b"rhythm-llm-key-fingerprint-hmac-sha256-v1"


class KeyCipherUnavailable(Exception):
    """Raised when the encryption secret is missing or malformed."""


class KeyCipher:
    def __init__(self, secret_b64: str) -> None:
        if not secret_b64 or not secret_b64.strip():
            raise KeyCipherUnavailable("Chưa cấu hình RHYTHM_SERVICE_KEY_ENCRYPTION_SECRET.")
        try:
            secret = base64.b64decode(secret_b64.strip(), validate=True)
        except (binascii.Error, ValueError):
            raise KeyCipherUnavailable("RHYTHM_SERVICE_KEY_ENCRYPTION_SECRET không phải base64 hợp lệ.") from None
        if len(secret) != 32:
            raise KeyCipherUnavailable("RHYTHM_SERVICE_KEY_ENCRYPTION_SECRET phải giải mã ra đúng 32 byte.")
        # Independent sub-keys so the AES key is never reused as the HMAC key.
        self._aes = AESGCM(hmac.new(secret, _AES_CONTEXT, hashlib.sha256).digest())
        self._hmac_key = hmac.new(secret, _HMAC_CONTEXT, hashlib.sha256).digest()

    def encrypt(self, plain: str) -> tuple[bytes, bytes]:
        nonce = os.urandom(_NONCE_BYTES)
        return self._aes.encrypt(nonce, plain.encode("utf-8"), None), nonce

    def decrypt(self, ciphertext: bytes, nonce: bytes) -> str:
        return self._aes.decrypt(nonce, ciphertext, None).decode("utf-8")

    def fingerprint(self, plain: str) -> str:
        return hmac.new(self._hmac_key, plain.encode("utf-8"), hashlib.sha256).hexdigest()
