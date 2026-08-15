"""Ajustes globales del panel (tabla KV app_settings)

Revision ID: 0044_app_settings
Revises: 0043_backups
Create Date: 2026-07-14

Tabla clave-valor pequeña para settings de despliegue que no encajan en una
tabla propia ni son por-agente. Primer uso: `dashboard_timezone` (zona horaria
del dashboard/Home, editable desde Ajustes). No se siembra ninguna fila: si la
clave no existe, el backend cae al default de config (Europe/Madrid).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0044_app_settings"
down_revision = "0043_backups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "app_settings" not in set(inspector.get_table_names()):
        op.create_table(
            "app_settings",
            sa.Column("key", sa.String(80), primary_key=True),
            sa.Column("value", sa.Text(), nullable=False),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )


def downgrade() -> None:
    op.drop_table("app_settings")
