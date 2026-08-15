"""archive conversations from inbox

Revision ID: 0006_conversation_archived
Revises: 0005_agent_monthly_budget
Create Date: 2026-05-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_conversation_archived"
down_revision = "0005_agent_monthly_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_conversations_archived_at",
        "conversations",
        ["archived_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_archived_at", "conversations")
    op.drop_column("conversations", "archived_at")
