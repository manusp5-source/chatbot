"""Copias de seguridad: destino ELEGIBLE (bucket / servidor / ambos)

Revision ID: 0047_backup_destination_choice
Revises: 0046_backup_destino
Create Date: 2026-07-17

backup_settings.destination: cloud | server | both — dónde acaba cada copia
(selector del panel). Se deja NULL a propósito en instalaciones existentes:
NULL = comportamiento histórico (dump en el servidor + subida si hay bucket)
hasta que el usuario elija y guarde en el panel.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0047_backup_destination_choice"
down_revision = "0046_backup_destino"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotente por si una corrida previa quedó a medias.
    bind = op.get_bind()
    cols = [c["name"] for c in inspect(bind).get_columns("backup_settings")]
    if "destination" not in cols:
        op.add_column(
            "backup_settings",
            sa.Column("destination", sa.String(length=16), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("backup_settings", "destination")
