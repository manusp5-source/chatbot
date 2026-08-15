"""create agent_corrections (autoaprendizaje Fase 1: corrige y re-redacta)

Revision ID: 0026_agent_correction
Revises: 0025_outbound_jobs
Create Date: 2026-06-16

Registra cada corrección de la operadora al agente (instrucción en lenguaje
natural) y la re-redacción resultante. Materia prima de la Fase 2.
Textos cifrados en reposo (EncryptedText → BYTEA).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "0026_agent_correction"
down_revision = "0025_outbound_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotente por si una corrida previa creó la tabla a medias.
    bind = op.get_bind()
    if "agent_corrections" not in inspect(bind).get_table_names():
        op.create_table(
            "agent_corrections",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "conversation_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("canal", sa.String(length=40), nullable=True),
            # Textos cifrados en reposo (Fernet) — tipo subyacente BYTEA.
            sa.Column("original_text", sa.LargeBinary(), nullable=True),
            sa.Column("instruction", sa.LargeBinary(), nullable=False),
            sa.Column("resulting_text", sa.LargeBinary(), nullable=True),
            sa.Column(
                "created_by",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_agent_corrections_conversation_id",
            "agent_corrections",
            ["conversation_id"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_corrections_conversation_id", table_name="agent_corrections"
    )
    op.drop_table("agent_corrections")
