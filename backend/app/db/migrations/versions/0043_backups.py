"""Copias de seguridad: configuración + registro de intentos

Revision ID: 0043_backups
Revises: 0042_llm_model_price
Create Date: 2026-07-14

Sistema de copias: pg_dump cifrado (AES-256-GCM)
subido a un bucket S3-compatible con retención y registro de intentos.

- backup_settings: fila única (frecuencia, hora de la copia diaria, proveedor,
  enabled). Se siembra con los defaults de la spec (diaria a las 04:00
  Europe/Madrid). Las credenciales S3 van en `credentials` (backup_s3_*).
- backup_runs: un registro por intento (scheduled/manual/restore) para el
  estado "última copia" y el histórico de la UI.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID


revision = "0043_backups"
down_revision = "0042_llm_model_price"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "backup_settings" not in tables:
        op.create_table(
            "backup_settings",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("frequency", sa.String(16), nullable=False, server_default="daily"),
            sa.Column("daily_hour", sa.Integer(), nullable=False, server_default="4"),
            sa.Column("provider", sa.String(24), nullable=False, server_default="r2"),
            sa.Column("last_scheduled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )
        # Singleton sembrado: el panel edita SIEMPRE esta fila (no crea otras).
        op.execute(
            "INSERT INTO backup_settings (id, enabled, frequency, daily_hour, provider) "
            "VALUES (gen_random_uuid(), true, 'daily', 4, 'r2')"
        )

    if "backup_runs" not in tables:
        op.create_table(
            "backup_runs",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("s3_key", sa.String(255), nullable=True),
            sa.Column("size_bytes", sa.BigInteger(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )
        op.create_index("ix_backup_runs_created_at", "backup_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_backup_runs_created_at", table_name="backup_runs")
    op.drop_table("backup_runs")
    op.drop_table("backup_settings")
