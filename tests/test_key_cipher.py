import base64
import os

import pytest

from app.security.key_cipher import KeyCipher, KeyCipherUnavailable


def _secret() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def test_round_trip():
    cipher = KeyCipher(_secret())
    assert cipher.decrypt(*cipher.encrypt("AIza-secret")) == "AIza-secret"


def test_nonce_is_random():
    cipher = KeyCipher(_secret())
    first, first_nonce = cipher.encrypt("AIza-secret")
    second, second_nonce = cipher.encrypt("AIza-secret")
    assert first != second
    assert first_nonce != second_nonce
    assert len(first_nonce) == 12


def test_wrong_secret_cannot_decrypt():
    ciphertext, nonce = KeyCipher(_secret()).encrypt("AIza-secret")
    with pytest.raises(Exception) as info:
        KeyCipher(_secret()).decrypt(ciphertext, nonce)
    assert "AIza-secret" not in str(info.value)


def test_fingerprint_stable_and_secret_bound():
    secret = _secret()
    first = KeyCipher(secret).fingerprint("AIza-secret")
    assert first == KeyCipher(secret).fingerprint("AIza-secret")
    assert first != KeyCipher(_secret()).fingerprint("AIza-secret")
    assert first != KeyCipher(secret).fingerprint("AIza-other")
    assert len(first) == 64
    int(first, 16)


def test_ciphertext_does_not_contain_plain():
    ciphertext, _ = KeyCipher(_secret()).encrypt("AIza-secret")
    assert b"AIza-secret" not in ciphertext


@pytest.mark.parametrize("secret", ["", "not-base64!!", base64.b64encode(os.urandom(16)).decode()])
def test_invalid_secret_raises_unavailable(secret):
    with pytest.raises(KeyCipherUnavailable):
        KeyCipher(secret)
