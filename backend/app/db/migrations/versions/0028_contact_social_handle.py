"""add contacts.social_handle (Instagram: @usuario aparte del nombre real)

Revision ID: 0028_contact_social_handle
Revises: 0027_merge_heads
Create Date: 2026-06-16

Para mostrar en la bandeja el nombre real como título y el @usuario debajo.
Solo lo rellena Instagram; NULL en el resto de canales.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0028_contact_social_handle"
down_revision = "0027_merge_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotente por si una corrida previa añadió la columna a medias.
    bind = op.get_bind()
    cols = [c["name"] for c in inspect(bind).get_columns("contacts")]
    if "social_handle" not in cols:
        op.add_column(
            "contacts", sa.Column("social_handle", sa.String(length=80), nullable=True)
        )


def downgrade() -> None:
    op.drop_column("contacts", "social_handle")
