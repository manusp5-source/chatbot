"""agent_corrections.analysis_attempts — intentos de análisis fallidos

Revision ID: 0048_agent_correction_attempts
Revises: 0047_backup_destination_choice
Create Date: 2026-07-25

El detector de correcciones repetidas marcaba TODAS las correcciones como
procesadas al final de la corrida, incluso las de un grupo cuya llamada al LLM
había fallado: esas correcciones no se volvían a analizar nunca (una caída de
OpenAI se comía el autoaprendizaje de esa hora). Ahora solo se cierran las
analizadas; las que fallan quedan pendientes y este contador evita el bucle
infinito con una corrección que siempre falla (se cierra a los N intentos).

NOT NULL con server_default 0: las filas existentes arrancan en 0.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0048_agent_correction_attempts"
down_revision = "0047_backup_destination_choice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "agent_corrections" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("agent_corrections")}
        if "analysis_attempts" not in cols:
            op.add_column(
                "agent_corrections",
                sa.Column(
                    "analysis_attempts",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "agent_corrections" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("agent_corrections")}
        if "analysis_attempts" in cols:
            op.drop_column("agent_corrections", "analysis_attempts")
