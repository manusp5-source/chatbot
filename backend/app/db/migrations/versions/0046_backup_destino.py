"""Copias de seguridad: destino por copia (servidor / bucket)

Revision ID: 0046_backup_destino
Revises: 0045_contact_crm_fields
Create Date: 2026-07-16

Para que el panel deje inequívoco DÓNDE quedó cada copia:

- backup_runs.local_file: nombre del dump en el disco del servidor (NULL si
  el dump falló o en restauraciones).
- backup_runs.upload_status: uploaded | failed | not_configured — estado de
  la subida al bucket de ESTA copia (NULL en filas antiguas/restores).

Las frecuencias nuevas (weekly/monthly) no necesitan cambio de esquema:
backup_settings.frequency ya es String(16).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0046_backup_destino"
down_revision = "0045_contact_crm_fields"
branch_labels = None
depends_on = None

_COLUMNS = [
    ("local_file", sa.String(length=255)),
    ("upload_status", sa.String(length=16)),
]


def upgrade() -> None:
    # Idempotente por si una corrida previa añadió columnas a medias.
    bind = op.get_bind()
    cols = [c["name"] for c in inspect(bind).get_columns("backup_runs")]
    for name, coltype in _COLUMNS:
        if name not in cols:
            op.add_column("backup_runs", sa.Column(name, coltype, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("backup_runs", name)
