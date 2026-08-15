"""email channel (Gmail) — Fase 5a

Revision ID: 0015_email_channel
Revises: 0014_agent_prompt_history
Create Date: 2026-06-03

Canal Email (Gmail) en solo lectura. Añade el valor 'email' a los tres enums
(channel_type, conversation_canal, contact_origen) y dos columnas a
`conversations` para agrupar por hilo de Gmail:
  - gmail_thread_id: el threadId de Gmail (agrupa los mensajes del mismo hilo
    en una sola conversación).
  - subject: el asunto del correo (se muestra en cabecera/preview del inbox).

ALTER TYPE ADD VALUE no funciona dentro de una transacción en Postgres < 12;
usamos autocommit_block (mismo patrón que 0010_instagram_enum_values).
"""
from alembic import op
import sqlalchemy as sa


revision = "0015_email_channel"
down_revision = "0014_agent_prompt_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------- Enums (fuera de transacción, idempotente) ----------
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE channel_type ADD VALUE IF NOT EXISTS 'email'")
        op.execute("ALTER TYPE conversation_canal ADD VALUE IF NOT EXISTS 'email'")
        op.execute("ALTER TYPE contact_origen ADD VALUE IF NOT EXISTS 'email'")

    # ---------- Columnas de agrupación por hilo en conversations ----------
    op.add_column(
        "conversations",
        sa.Column("gmail_thread_id", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_conversations_gmail_thread_id",
        "conversations",
        ["gmail_thread_id"],
    )
    op.add_column(
        "conversations",
        sa.Column("subject", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # Las columnas sí se revierten. Los ALTER TYPE ADD VALUE no se deshacen
    # fácilmente en Postgres (habría que recrear el enum entero), así que el
    # valor 'email' de los enums se deja — mismo criterio que 0010.
    op.drop_index("ix_conversations_gmail_thread_id", table_name="conversations")
    op.drop_column("conversations", "subject")
    op.drop_column("conversations", "gmail_thread_id")
