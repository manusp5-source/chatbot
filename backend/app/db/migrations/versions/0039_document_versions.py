"""Versionado de documentos de la base de conocimiento

Revision ID: 0039_document_versions
Revises: 0038_fallback_providers
Create Date: 2026-07-04

Al editar un documento de texto (txt/md) desde el panel, el contenido anterior
se guarda en document_versions para poder restaurarlo. El contenido va en claro
por el mismo motivo que los chunks: es material de la KB, no PII, y las
versiones deben poder inspeccionarse/restaurarse tal cual.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID


revision = "0039_document_versions"
down_revision = "0038_fallback_providers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "document_versions" not in set(inspector.get_table_names()):
        op.create_table(
            "document_versions",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "document_id",
                UUID(as_uuid=True),
                sa.ForeignKey("documents.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("contenido", sa.Text(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "created_by",
                UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_document_versions_document_id", "document_versions", ["document_id"]
        )


def downgrade() -> None:
    op.drop_index("ix_document_versions_document_id", table_name="document_versions")
    op.drop_table("document_versions")
