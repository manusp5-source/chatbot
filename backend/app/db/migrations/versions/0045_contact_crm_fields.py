"""Contactos como mini-CRM: empresa, cargo, web, NIF/CIF y dirección.

Revision ID: 0045_contact_crm_fields
Revises: 0044_app_settings
Create Date: 2026-07-15

Campos opcionales de ficha CRM. NIF/CIF y dirección son PII fiscal/postal y
van cifrados en reposo (EncryptedText → BYTEA). Empresa, cargo y web quedan
en claro para poder buscarse/filtrarse en SQL.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0045_contact_crm_fields"
down_revision = "0044_app_settings"
branch_labels = None
depends_on = None

_PLAIN = [
    ("empresa", sa.String(length=200)),
    ("cargo", sa.String(length=120)),
    ("web", sa.String(length=255)),
]
# Cifrados con Fernet (EncryptedText en el modelo) → BYTEA en BD.
_ENCRYPTED = ["nif", "direccion"]


def upgrade() -> None:
    # Idempotente por si una corrida previa añadió columnas a medias.
    bind = op.get_bind()
    cols = [c["name"] for c in inspect(bind).get_columns("contacts")]
    for name, coltype in _PLAIN:
        if name not in cols:
            op.add_column("contacts", sa.Column(name, coltype, nullable=True))
    for name in _ENCRYPTED:
        if name not in cols:
            op.add_column("contacts", sa.Column(name, sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    for name in _ENCRYPTED:
        op.drop_column("contacts", name)
    for name, _ in reversed(_PLAIN):
        op.drop_column("contacts", name)
