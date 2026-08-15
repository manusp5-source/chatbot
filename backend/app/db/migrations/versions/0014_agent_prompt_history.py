"""agent_prompt_history

Revision ID: 0014_agent_prompt_history
Revises: 0013_classifier
Create Date: 2026-06-02

Historial de prompts por agente (#11). Tabla vacía al inicio: se llena cuando
el prompt de un agente cambia (se guarda el valor anterior).
"""
from alembic import op
import sqlalchemy as sa


revision = "0014_agent_prompt_history"
down_revision = "0013_classifier"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_prompt_history",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("prompt_system", sa.Text, nullable=False),
        sa.Column("model_name", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "created_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_agent_prompt_history_agent_id", "agent_prompt_history", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_prompt_history_agent_id", table_name="agent_prompt_history")
    op.drop_table("agent_prompt_history")
