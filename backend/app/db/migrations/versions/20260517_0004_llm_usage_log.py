"""create llm_usage_log

Revision ID: 20260517_0004
Revises: 20260517_0003
Create Date: 2026-05-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_llm_usage_log"
down_revision = "0003_agent_handoff_message"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_usage_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_llm_usage_log_created_at", "llm_usage_log", ["created_at"])
    op.create_index("ix_llm_usage_log_source_created_at", "llm_usage_log", ["source", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_log_source_created_at", "llm_usage_log")
    op.drop_index("ix_llm_usage_log_created_at", "llm_usage_log")
    op.drop_table("llm_usage_log")
