"""Tests for services/db_service.py encryption helpers."""

import pytest
from cryptography.exceptions import InvalidTag

from services.db_service import _hash_vault_key, decrypt, derive_key, encrypt


class TestDeriveKey:
    def test_returns_32_bytes(self):
        key = derive_key("test_vault_key", "test_salt")
        assert len(key) == 32
        assert isinstance(key, bytes)

    def test_deterministic(self):
        k1 = derive_key("password", "salt")
        k2 = derive_key("password", "salt")
        assert k1 == k2

    def test_different_salt_different_key(self):
        k1 = derive_key("password", "salt_a")
        k2 = derive_key("password", "salt_b")
        assert k1 != k2

    def test_different_password_different_key(self):
        k1 = derive_key("password_a", "salt")
        k2 = derive_key("password_b", "salt")
        assert k1 != k2


class TestEncryptDecrypt:
    def test_roundtrip(self):
        key = derive_key("vault", "salt")
        plaintext = "Hello, World!"
        ciphertext = encrypt(plaintext, key)
        assert decrypt(ciphertext, key) == plaintext

    def test_roundtrip_unicode(self):
        key = derive_key("vault", "salt")
        plaintext = '{"name": "राम", "status": "ACTIVE"}'
        ciphertext = encrypt(plaintext, key)
        assert decrypt(ciphertext, key) == plaintext

    def test_roundtrip_json(self):
        key = derive_key("vault", "salt")
        import json
        data = {"policy_number": "123", "premium": 5000, "status": "ACTIVE"}
        plaintext = json.dumps(data)
        ciphertext = encrypt(plaintext, key)
        result = json.loads(decrypt(ciphertext, key))
        assert result == data

    def test_different_ciphertexts_each_call(self):
        key = derive_key("vault", "salt")
        c1 = encrypt("same text", key)
        c2 = encrypt("same text", key)
        assert c1 != c2  # random nonce

    def test_wrong_key_raises(self):
        key1 = derive_key("correct", "salt")
        key2 = derive_key("wrong", "salt")
        ciphertext = encrypt("secret", key1)
        with pytest.raises(InvalidTag):
            decrypt(ciphertext, key2)

    def test_corrupted_ciphertext_raises(self):
        key = derive_key("vault", "salt")
        with pytest.raises(Exception):
            decrypt("not_valid_base64!!!", key)


class TestHashVaultKey:
    def test_deterministic(self):
        h1 = _hash_vault_key("key", "salt")
        h2 = _hash_vault_key("key", "salt")
        assert h1 == h2

    def test_different_salt(self):
        h1 = _hash_vault_key("key", "salt_a")
        h2 = _hash_vault_key("key", "salt_b")
        assert h1 != h2

    def test_returns_hex_string(self):
        h = _hash_vault_key("key", "salt")
        assert len(h) == 64  # SHA-256 hex digest
        assert all(c in "0123456789abcdef" for c in h)
