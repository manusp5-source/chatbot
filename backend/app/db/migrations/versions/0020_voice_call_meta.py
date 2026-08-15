"""voice call metadata on conversations (Registro de llamadas, Retell)

Revision ID: 0020_voice_call_meta
Revises: 0019_retell_voice_enum
Create Date: 2026-06-08

La sección "Llamadas" (canal de voz Retell) muestra, por cada llamada,
duración, motivo de fin y la grabación. Esos datos NO llegan turno a turno por
el WebSocket del Custom LLM, sino por el webhook de ciclo de vida de la llamada
(`call_ended` / `call_analyzed`). Añadimos tres columnas NULL a `conversations`
para guardarlos (el resumen reutiliza `conversations.resumen`, el fin reutiliza
`conversations.ended_at`):

  - call_duration_seconds: duración de la llamada en segundos.
  - call_recording_url:    URL de la grabación en Retell (NO se descarga; puede
                           caducar — es un enlace de Retell).
  - call_ended_reason:     motivo de fin (disconnection_reason de Retell).

Son genéricas a nivel de columna pero solo se rellenan para conversaciones de
voz; en el resto de canales quedan NULL.
"""
from alembic import op
import sqlalchemy as sa


revision = "0020_voice_call_meta"
down_revision = "0019_retell_voice_enum"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("call_duration_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("call_recording_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("call_ended_reason", sa.String(80), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "call_ended_reason")
    op.drop_column("conversations", "call_recording_url")
    op.drop_column("conversations", "call_duration_seconds")
