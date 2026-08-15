"""add 'retell_voice' value to conversation_canal enum (Voz, V-01)

Revision ID: 0019_retell_voice_enum
Revises: 0018_whatsapp_template_var
Create Date: 2026-06-03

El canal de voz (Retell) persiste sus llamadas en `conversations` con
`canal = 'retell_voice'` (ver app/api/voice.py). El tipo enum de Postgres
`conversation_canal` se creó como ('whatsapp','web') y solo se le añadieron
'instagram_dm' (0010) y 'email' (0015). Faltaba 'retell_voice', así que el
modelo Python referenciaba un valor inexistente en BD y cada turno real de
voz fallaba al insertar la Conversation.

`channel_type` (la otra tabla) YA tenía 'retell_voice' desde 0009; esto solo
toca `conversation_canal`.

ALTER TYPE ADD VALUE no funciona dentro de una transacción en Postgres < 12;
usamos autocommit_block (mismo patrón que 0010_instagram_enum_values y
0015_email_channel). IF NOT EXISTS lo hace idempotente.
"""
from alembic import op


revision = "0019_retell_voice_enum"
down_revision = "0018_whatsapp_template_var"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE conversation_canal ADD VALUE IF NOT EXISTS 'retell_voice'")


def downgrade() -> None:
    # Postgres no permite DROP VALUE de un enum sin recrearlo entero. Como es
    # una operación cara y casi nunca necesaria, dejamos el downgrade noop
    # (mismo criterio que 0010 y 0015).
    pass
