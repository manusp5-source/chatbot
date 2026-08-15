"""add llm_usage_log.agent_id (desglose de consumo por agente)

Revision ID: 0026_llm_usage_agent_id
Revises: 0025_outbound_jobs
Create Date: 2026-06-16

El dashboard de agentes muestra el consumo de tokens. Para poder desglosarlo
"por agente" (qué Agente del modelo multi-agente consume más, además del
clasificador y el agente interno, que ya se distinguen por `source`), añadimos
`agent_id` a `llm_usage_log`. Solo se rellena en las llamadas del agente
conversacional (source='agent'); el resto queda NULL y se atribuye por `source`.

Sin FK a `agents`: si un Agent se borra, conservamos el id para no perder el
histórico de gasto (el endpoint resuelve el nombre por lookup, con fallback).
La columna es nullable → migración no destructiva; el histórico previo queda
como "sin asignar".
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "0026_llm_usage_agent_id"
down_revision = "0025_outbound_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in inspect(bind).get_columns("llm_usage_log")}
    if "agent_id" not in cols:
        op.add_column(
            "llm_usage_log",
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_index("ix_llm_usage_log_agent_id", "llm_usage_log", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_log_agent_id", "llm_usage_log")
    op.drop_column("llm_usage_log", "agent_id")
