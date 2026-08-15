"""Propuestas de edición de KB del agente interno (Aplicar/Descartar)

Revision ID: 0040_kb_edit_proposals
Revises: 0039_document_versions
Create Date: 2026-07-04

El agente interno propone cambios en la base de conocimiento; el admin los
aplica o descarta desde el chat. El agente nunca escribe la KB directamente.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID


revision = "0040_kb_edit_proposals"
down_revision = "0039_document_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "kb_edit_proposals" not in set(inspector.get_table_names()):
        op.create_table(
            "kb_edit_proposals",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("kind", sa.String(10), nullable=False),
            sa.Column(
                "document_id",
                UUID(as_uuid=True),
                sa.ForeignKey("documents.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("titulo", sa.String(255), nullable=True),
            sa.Column("contenido", sa.Text(), nullable=False),
            sa.Column("motivo", sa.Text(), nullable=True),
            sa.Column("status", sa.String(12), nullable=False, server_default="pendiente"),
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
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "resolved_by",
                UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "applied_document_id",
                UUID(as_uuid=True),
                sa.ForeignKey("documents.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index("ix_kb_edit_proposals_status", "kb_edit_proposals", ["status"])


def downgrade() -> None:
    op.drop_index("ix_kb_edit_proposals_status", table_name="kb_edit_proposals")
    op.drop_table("kb_edit_proposals")
