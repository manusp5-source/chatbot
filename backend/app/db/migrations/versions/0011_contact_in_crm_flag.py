"""add contacts.in_crm flag

Revision ID: 0011_contact_in_crm_flag
Revises: 0010_instagram_enum_values
Create Date: 2026-05-18

El administrador no quiere que los contactos de Instagram (gente que escribe DM)
contaminen el CRM automáticamente. Solo deben aparecer en la lista de
Contactos cuando el administrador los promueva manualmente.

`in_crm` se usa para filtrar en la API de /contacts. Para no romper nada
de los contactos ya creados (WhatsApp legacy), se rellena a TRUE por
defecto en el upgrade.
"""
from alembic import op
import sqlalchemy as sa


revision = "0011_contact_in_crm_flag"
down_revision = "0010_instagram_enum_values"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column(
            "in_crm",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.create_index("ix_contacts_in_crm", "contacts", ["in_crm"])


def downgrade() -> None:
    op.drop_index("ix_contacts_in_crm", "contacts")
    op.drop_column("contacts", "in_crm")
