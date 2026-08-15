"""Tipo SQLAlchemy que cifra/descifra texto transparentemente con Fernet.

Aplicar a campos PII no-searchable de longitud variable (notas, transcripts,
resúmenes). NO usar en columnas que necesiten filtros SQL exactos o LIKE: el
texto cifrado es opaco para la BD.

Trade-off consciente: campos como teléfono/email/nombre se dejan en claro
porque la app los necesita searchable y/o unique. La forma profesional de
cifrarlos sería con HMAC determinista para búsqueda exacta + Fernet para
almacenamiento, pero rompe búsquedas LIKE y complica la app. Para fase 1
asumimos que esa PII está protegida por:
  - cifrado a nivel volumen del disco (EasyPanel / cloud provider),
  - controles de acceso al panel admin,
  - cifrado de credenciales de BD.
"""
from __future__ import annotations

from sqlalchemy import LargeBinary
from sqlalchemy.types import TypeDecorator

from app.core.encryption import get_encryption_service


class EncryptedText(TypeDecorator):
    """Texto cifrado en reposo con Fernet. Tipo subyacente: BYTEA."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, str):
            value = str(value)
        return get_encryption_service().encrypt(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        try:
            return get_encryption_service().decrypt(value)
        except Exception:
            # Si una fila legacy quedó sin cifrar o con clave distinta,
            # devolvemos un marcador en lugar de romper la fila entera.
            return None
