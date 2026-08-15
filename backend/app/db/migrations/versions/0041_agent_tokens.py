"""Tokens de agente para la API de agentes externos (/agent-api/v1)

Revision ID: 0041_agent_tokens
Revises: 0040_kb_edit_proposals
Create Date: 2026-07-04

Credenciales con ámbitos (monitor:read, kb:read, kb:write, prompts:write)
para los asistentes propios del operador. Se guarda
solo el SHA-256 del token; el valor en claro se muestra una única vez.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID


revision = "0041_agent_tokens"
down_revision = "0040_kb_edit_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "agent_tokens" not in set(inspector.get_table_names()):
        op.create_table(
            "agent_tokens",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("token_prefix", sa.String(16), nullable=False),
            sa.Column("scopes", sa.String(200), nullable=False, server_default=""),
            sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_by",
                UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_agent_tokens_token_hash", "agent_tokens", ["token_hash"], unique=True
        )


def downgrade() -> None:
    op.drop_index("ix_agent_tokens_token_hash", table_name="agent_tokens")
    op.drop_table("agent_tokens")
