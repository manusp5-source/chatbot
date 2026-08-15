"""Cifra en reposo audio_transcript, notas_internas y resumen.

Cambia los tipos de columna TEXT → BYTEA. Si existen datos en claro
previos, se cifran in-place usando la `ENCRYPTION_KEY` de la app.

Revision ID: 0002_encrypt_pii
Revises: 0001_initial
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "0002_encrypt_pii"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _migrate_text_to_encrypted(table: str, column: str) -> None:
    """Cifra datos existentes y cambia el tipo a BYTEA.

    Hacemos el cifrado en Python (no podemos cifrar Fernet desde SQL) usando
    el `EncryptionService` de la app, que lee ENCRYPTION_KEY de las settings.
    """
    from app.core.encryption import get_encryption_service

    bind = op.get_bind()
    # 1) Renombrar columna actual TEXT a *_plain temporal
    op.alter_column(table, column, new_column_name=f"{column}_plain")
    # 2) Crear nueva columna BYTEA
    op.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} BYTEA"))
    # 3) Cifrar fila a fila lo que no sea NULL
    enc = get_encryption_service()
    rows = bind.execute(text(f"SELECT id, {column}_plain FROM {table} WHERE {column}_plain IS NOT NULL")).fetchall()
    for row in rows:
        ciphertext = enc.encrypt(row[1])
        bind.execute(
            text(f"UPDATE {table} SET {column} = :ct WHERE id = :id"),
            {"ct": ciphertext, "id": row[0]},
        )
    # 4) Borrar columna en claro
    op.drop_column(table, f"{column}_plain")


def _migrate_encrypted_to_text(table: str, column: str) -> None:
    """Inverso: descifra y vuelve a TEXT (downgrade)."""
    from app.core.encryption import get_encryption_service

    bind = op.get_bind()
    op.alter_column(table, column, new_column_name=f"{column}_enc")
    op.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} TEXT"))
    enc = get_encryption_service()
    rows = bind.execute(text(f"SELECT id, {column}_enc FROM {table} WHERE {column}_enc IS NOT NULL")).fetchall()
    for row in rows:
        try:
            plain = enc.decrypt(bytes(row[1]))
        except Exception:
            plain = None
        if plain is not None:
            bind.execute(
                text(f"UPDATE {table} SET {column} = :pt WHERE id = :id"),
                {"pt": plain, "id": row[0]},
            )
    op.drop_column(table, f"{column}_enc")


def upgrade() -> None:
    _migrate_text_to_encrypted("messages", "audio_transcript")
    _migrate_text_to_encrypted("contacts", "notas_internas")
    _migrate_text_to_encrypted("conversations", "resumen")


def downgrade() -> None:
    _migrate_encrypted_to_text("messages", "audio_transcript")
    _migrate_encrypted_to_text("contacts", "notas_internas")
    _migrate_encrypted_to_text("conversations", "resumen")
