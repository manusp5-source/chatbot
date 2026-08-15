"""widen identifier columns (contact.telefono, conversation.session_id)

Revision ID: 0024_widen_identifier_columns
Revises: 0023_unread_partial_index
Create Date: 2026-06-16

`Contact.telefono` se usa como identificador UNIVERSAL de todos los canales, con
prefijo: 'email:<address>', 'ig:<psid>', 'web:<uuid>' y el teléfono real. Estaba
en VARCHAR(40) (pensado para teléfonos), pero una dirección de email larga lo
desborda — p.ej. 'email:mailer-daemon@eu-west-1.amazonses.com' (43 car.) — y
revienta el INSERT del contacto. Como el sondeo de Gmail procesaba los correos en
el mismo bucle, esa excepción ABORTABA TODO el sondeo y dejaba de entrar correo.

Ampliamos a 320 (cubre el máximo RFC de email, 254, + el prefijo). Igual para
`Conversation.session_id` (VARCHAR(80)), que guarda el mismo identificador.
"""
from alembic import op
import sqlalchemy as sa

revision = "0024_widen_identifier_columns"
down_revision = "0023_unread_partial_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "contacts",
        "telefono",
        existing_type=sa.String(length=40),
        type_=sa.String(length=320),
        existing_nullable=False,
    )
    op.alter_column(
        "conversations",
        "session_id",
        existing_type=sa.String(length=80),
        type_=sa.String(length=320),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "conversations",
        "session_id",
        existing_type=sa.String(length=320),
        type_=sa.String(length=80),
        existing_nullable=False,
    )
    op.alter_column(
        "contacts",
        "telefono",
        existing_type=sa.String(length=320),
        type_=sa.String(length=40),
        existing_nullable=False,
    )
