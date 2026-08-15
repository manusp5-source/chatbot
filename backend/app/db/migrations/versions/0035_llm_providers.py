"""llm_providers + binding por agente (proveedores LLM configurables)

Revision ID: 0035_llm_providers
Revises: 0034_user_password_changed_at
Create Date: 2026-07-01

Crea la tabla `llm_providers` (proveedores compatibles con la API de OpenAI,
configurables desde el panel), siembra el proveedor por defecto "OpenAI"
(api_key vacía → usa la credencial openai_api_key existente, retrocompat) y
añade `llm_provider_id` (nullable, ON DELETE SET NULL) a `agents`,
`classifier_config` e `internal_agent_config`. NULL = proveedor por defecto,
así que los agentes existentes siguen funcionando sin cambios.
"""
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "0035_llm_providers"
down_revision = "0034_user_password_changed_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "llm_providers" not in tables:
        op.create_table(
            "llm_providers",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(80), nullable=False, unique=True),
            sa.Column("base_url", sa.String(300), nullable=True),
            sa.Column("api_key", sa.LargeBinary(), nullable=True),
            sa.Column(
                "accepts_temperature", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
        )
        # Semilla: OpenAI por defecto. api_key NULL → cae a openai_api_key.
        # accepts_temperature=False: los modelos por defecto son GPT-5.x
        # (reasoning), que no admiten temperature.
        op.execute(
            sa.text(
                "INSERT INTO llm_providers (id, name, base_url, api_key, "
                "accepts_temperature, is_default) "
                "VALUES (:id, 'OpenAI', NULL, NULL, false, true)"
            ).bindparams(id=str(uuid.uuid4()))
        )

    for table in ("agents", "classifier_config", "internal_agent_config"):
        if table in tables:
            cols = {c["name"] for c in inspector.get_columns(table)}
            if "llm_provider_id" not in cols:
                op.add_column(
                    table,
                    sa.Column(
                        "llm_provider_id",
                        postgresql.UUID(as_uuid=True),
                        sa.ForeignKey("llm_providers.id", ondelete="SET NULL"),
                        nullable=True,
                    ),
                )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    for table in ("agents", "classifier_config", "internal_agent_config"):
        if table in tables:
            cols = {c["name"] for c in inspector.get_columns(table)}
            if "llm_provider_id" in cols:
                op.drop_column(table, "llm_provider_id")
    if "llm_providers" in tables:
        op.drop_table("llm_providers")
