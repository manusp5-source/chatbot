from app.core.encryption import EncryptionService


def test_fernet_roundtrip():
    enc = EncryptionService()
    secret = "sk-abc-1234567890-xyz"
    ciphertext = enc.encrypt(secret)
    assert ciphertext != secret.encode()
    assert enc.decrypt(ciphertext) == secret
