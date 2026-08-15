"""create contact_note (notas multi-autor)

Revision ID: 0016_contact_note
Revises: 0015_email_channel
Create Date: 2026-06-03

Notas internas multi-autor de la ficha de contacto. Pasamos del campo único
`contacts.notas_internas` (texto cifrado) a una LISTA de notas, cada una con
autor, fecha y texto.

Decisión del dueño — EMPEZAR DE CERO:
  - NO se migra el contenido de `contacts.notas_internas` a las nuevas notas.
  - NO se borra la columna `contacts.notas_internas` (queda archivada en BD).
La tabla nueva arranca vacía.

El texto va cifrado en reposo (mismo EncryptedText que notas_internas → BYTEA).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "0016_contact_note"
down_revision = "0015_email_channel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotente por si una corrida previa creó la tabla a medias.
    bind = op.get_bind()
    if "contact_note" not in inspect(bind).get_table_names():
        op.create_table(
            "contact_note",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "contact_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("contacts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "author_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            # Texto cifrado en reposo (Fernet) — tipo subyacente BYTEA.
            sa.Column("texto", sa.LargeBinary(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index("ix_contact_note_contact_id", "contact_note", ["contact_id"])

    # NOTA: contacts.notas_internas NO se toca (se conserva archivada).


def downgrade() -> None:
    op.drop_index("ix_contact_note_contact_id", table_name="contact_note")
    op.drop_table("contact_note")
