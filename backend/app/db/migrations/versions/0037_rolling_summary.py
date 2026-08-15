"""Resumen rodante de conversaciones largas

Revision ID: 0037_rolling_summary
Revises: 0036_hybrid_search
Create Date: 2026-07-01

Añade `conversations.rolling_summary` (BYTEA cifrado) y
`rolling_summary_upto` (int). La ventana de contexto (últimos N mensajes)
perdía el inicio de conversaciones largas (nombre, motivo). El resumen
rodante condensa los mensajes que caen fuera de la ventana y se inyecta en
el prompt del agente. `rolling_summary_upto` cuenta cuántos mensajes antiguos
ya están resumidos, para actualizar en incrementos.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0037_rolling_summary"
down_revision = "0036_hybrid_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "conversations" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("conversations")}
        if "rolling_summary" not in cols:
            op.add_column("conversations", sa.Column("rolling_summary", sa.LargeBinary(), nullable=True))
        if "rolling_summary_upto" not in cols:
            op.add_column(
                "conversations",
                sa.Column("rolling_summary_upto", sa.Integer(), nullable=False, server_default="0"),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "conversations" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("conversations")}
        if "rolling_summary_upto" in cols:
            op.drop_column("conversations", "rolling_summary_upto")
        if "rolling_summary" in cols:
            op.drop_column("conversations", "rolling_summary")
