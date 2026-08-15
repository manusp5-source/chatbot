"""add multimedia columns to messages

Revision ID: 0008_message_media_columns
Revises: 0007_agent_trace_event
Create Date: 2026-05-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_message_media_columns"
down_revision = "0007_agent_trace_event"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("media_type", sa.String(20), nullable=True))
    op.add_column("messages", sa.Column("media_url", sa.String(500), nullable=True))
    op.add_column("messages", sa.Column("media_mime", sa.String(120), nullable=True))
    op.add_column("messages", sa.Column("media_size", sa.Integer, nullable=True))
    op.add_column("messages", sa.Column("media_filename", sa.String(255), nullable=True))
    op.add_column("messages", sa.Column("media_duration_seconds", sa.Integer, nullable=True))
    op.add_column(
        "messages",
        sa.Column("media_purged_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("messages", "media_purged_at")
    op.drop_column("messages", "media_duration_seconds")
    op.drop_column("messages", "media_filename")
    op.drop_column("messages", "media_size")
    op.drop_column("messages", "media_mime")
    op.drop_column("messages", "media_url")
    op.drop_column("messages", "media_type")
