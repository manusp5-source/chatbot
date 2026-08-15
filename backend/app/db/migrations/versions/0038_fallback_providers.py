"""Fallback LLM como proveedor: global (flag en llm_providers) y por agente

Revision ID: 0038_fallback_providers
Revises: 0037_rolling_summary
Create Date: 2026-07-02

El fallback deja de ser 3 credenciales sueltas (llm_fallback_*) y pasa a ser
un PROVEEDOR de la tabla llm_providers:
  - llm_providers.is_fallback: el proveedor marcado es el respaldo GLOBAL.
  - llm_providers.fallback_model: modelo a usar cuando actúa de respaldo
    (solo aplica en la fila con is_fallback).
  - agents.fallback_provider_id / fallback_model: respaldo POR AGENTE
    (opcional; si no, aplica el global; si tampoco, las credenciales legacy
    llm_fallback_* siguen funcionando como compatibilidad).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID


revision = "0038_fallback_providers"
down_revision = "0037_rolling_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "llm_providers" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("llm_providers")}
        if "is_fallback" not in cols:
            op.add_column(
                "llm_providers",
                sa.Column("is_fallback", sa.Boolean(), nullable=False, server_default="false"),
            )
        if "fallback_model" not in cols:
            op.add_column(
                "llm_providers", sa.Column("fallback_model", sa.String(80), nullable=True)
            )

    if "agents" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("agents")}
        if "fallback_provider_id" not in cols:
            op.add_column(
                "agents",
                sa.Column(
                    "fallback_provider_id",
                    UUID(as_uuid=True),
                    sa.ForeignKey("llm_providers.id", ondelete="SET NULL"),
                    nullable=True,
                ),
            )
        if "fallback_model" not in cols:
            op.add_column("agents", sa.Column("fallback_model", sa.String(80), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "agents" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("agents")}
        if "fallback_model" in cols:
            op.drop_column("agents", "fallback_model")
        if "fallback_provider_id" in cols:
            op.drop_column("agents", "fallback_provider_id")
    if "llm_providers" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("llm_providers")}
        if "fallback_model" in cols:
            op.drop_column("llm_providers", "fallback_model")
        if "is_fallback" in cols:
            op.drop_column("llm_providers", "is_fallback")
