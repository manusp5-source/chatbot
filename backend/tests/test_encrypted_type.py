"""Roundtrip cifrado para EncryptedText.

Comprobamos que el tipo cifra al persistir (bind) y descifra al leer
(result), y que valores None pasan en claro como None.
"""
from app.core.encrypted_type import EncryptedText


def test_encrypted_type_roundtrip():
    t = EncryptedText()
    plain = "Información sensible del contacto: pedido #1234, alergia a tinte."
    ciphertext = t.process_bind_param(plain, dialect=None)
    assert ciphertext is not None
    assert isinstance(ciphertext, (bytes, bytearray))
    assert plain.encode("utf-8") not in ciphertext  # el texto no aparece en claro
    out = t.process_result_value(ciphertext, dialect=None)
    assert out == plain


def test_encrypted_type_handles_none():
    t = EncryptedText()
    assert t.process_bind_param(None, dialect=None) is None
    assert t.process_result_value(None, dialect=None) is None


def test_encrypted_type_corrupted_returns_none():
    """Si la fila contiene datos corruptos (clave distinta, etc.) devolvemos
    None en lugar de romper la query entera."""
    t = EncryptedText()
    assert t.process_result_value(b"not-a-valid-fernet-token", dialect=None) is None
