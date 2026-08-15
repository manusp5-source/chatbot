"""Cifrado simétrico Fernet para credenciales en DB."""

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class EncryptionService:
    def __init__(self, key: str | None = None) -> None:
        raw_key = (key or settings.ENCRYPTION_KEY).encode()
        if not raw_key:
            raise RuntimeError("ENCRYPTION_KEY no configurada")
        self._fernet = Fernet(raw_key)

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode()
        except InvalidToken as e:
            raise ValueError("No se pudo descifrar la credencial") from e


def get_encryption_service() -> EncryptionService:
    return EncryptionService()
