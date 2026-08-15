"""create knowledge_gaps (autoaprendizaje Fase 2 — huecos de conocimiento)

Revision ID: 0030_knowledge_gap
Revises: 0029_push_subscription
Create Date: 2026-06-16

Cuando el agente NO supo resolver (derivó a humano o la KB no tuvo resultados)
dejamos un hueco para que la operadora lo revise y lo convierta en conocimiento
permanente (siempre con aprobación humana). Los textos van cifrados en reposo
(EncryptedText → BYTEA / LargeBinary).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "0030_knowledge_gap"
down_revision = "0029_push_subscription"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotente por si una corrida previa creó la tabla a medias.
    bind = op.get_bind()
    if "knowledge_gaps" not in set(inspect(bind).get_table_names()):
        op.create_table(
            "knowledge_gaps",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            # SET NULL: conservamos la traza aunque se borre la conversación.
            sa.Column(
                "conversation_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("conversations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("trigger", sa.String(length=20), nullable=False),
            # Textos cifrados en reposo (Fernet) — tipo subyacente BYTEA.
            sa.Column("question", sa.LargeBinary(), nullable=False),
            sa.Column("suggested_answer", sa.LargeBinary(), nullable=True),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="pendiente",
            ),
            sa.Column(
                "created_document_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("documents.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "approved_by",
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
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_knowledge_gaps_status", "knowledge_gaps", ["status"])
        op.create_index(
            "ix_knowledge_gaps_conversation_id", "knowledge_gaps", ["conversation_id"]
        )


def downgrade() -> None:
    op.drop_index("ix_knowledge_gaps_conversation_id", table_name="knowledge_gaps")
    op.drop_index("ix_knowledge_gaps_status", table_name="knowledge_gaps")
    op.drop_table("knowledge_gaps")
