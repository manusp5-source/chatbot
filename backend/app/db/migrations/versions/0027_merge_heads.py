"""merge heads: 0026_agent_correction + 0026_llm_usage_agent_id

Revision ID: 0027_merge_heads
Revises: 0026_agent_correction, 0026_llm_usage_agent_id
Create Date: 2026-06-16

Dos ramas en paralelo crearon migraciones colgando AMBAS de 0025_outbound_jobs
(autoaprendizaje Fase 1 y tokens-por-agente) → dos cabezas → `alembic upgrade
head` falla con "Multiple head revisions" y el contenedor de la app entra en
bucle de reinicio (CPU al 100%).

Esta migración de FUSIÓN une las dos cabezas en una sola. No toca el esquema:
cada migración hija crea sus propias tablas/columnas; aquí solo unificamos el
grafo de revisiones.
"""
from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401


revision = "0027_merge_heads"
down_revision = ("0026_agent_correction", "0026_llm_usage_agent_id")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
