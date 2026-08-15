"""add instagram values to conversation_canal + contact_origen enums (F5)

Revision ID: 0010_instagram_enum_values
Revises: 0009_multichannel_tables
Create Date: 2026-05-18

ALTER TYPE ADD VALUE no funciona dentro de una transacción en Postgres < 12.
Usamos autocommit_block para garantizar el ALTER fuera del wrap transaccional
de alembic. En PG 12+ no hace falta pero no molesta.
"""
from alembic import op


revision = "0010_instagram_enum_values"
down_revision = "0009_multichannel_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE conversation_canal ADD VALUE IF NOT EXISTS 'instagram_dm'")
        op.execute("ALTER TYPE contact_origen ADD VALUE IF NOT EXISTS 'instagram'")


def downgrade() -> None:
    # Postgres no permite DROP VALUE de un enum sin recrearlo entero. Como es
    # una operación cara y casi nunca necesaria, dejamos el downgrade noop.
    pass
