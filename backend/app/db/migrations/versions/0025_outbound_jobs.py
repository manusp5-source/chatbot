"""create outbound_job + outbound_job_recipient (envío masivo en segundo plano)

Revision ID: 0025_outbound_jobs
Revises: 0024_widen_identifier_columns
Create Date: 2026-06-16

El envío masivo de plantillas pasa de ejecutarse síncronamente en la petición
HTTP (bloqueante, ~5 msg/s, riesgo de baneo) a un job en segundo plano que
manda los destinatarios "poco a poco" (retardo aleatorio entre envíos) vía una
tarea Celery que se re-encola con `countdown`.

`outbound_job` guarda la cabecera del envío + progreso (sent_ok/sent_error) y
los parámetros de ritmo (throttle_min/max_seconds). `outbound_job_recipient`
guarda una fila por destinatario con sus `variables` ya resueltas (array
ORDENADO, contrato `variables[idx-1] -> {{idx}}`) y su estado individual.

`status` se modela como String (no ENUM de Postgres) por simplicidad: son
estados internos de orquestación.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "0025_outbound_jobs"
down_revision = "0024_widen_identifier_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(inspect(bind).get_table_names())

    if "outbound_job" not in existing_tables:
        op.create_table(
            "outbound_job",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("template_name", sa.String(255), nullable=False),
            sa.Column("language", sa.String(20), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
            sa.Column("total", sa.Integer, nullable=False, server_default="0"),
            sa.Column("sent_ok", sa.Integer, nullable=False, server_default="0"),
            sa.Column("sent_error", sa.Integer, nullable=False, server_default="0"),
            sa.Column("throttle_min_seconds", sa.Integer, nullable=False, server_default="20"),
            sa.Column("throttle_max_seconds", sa.Integer, nullable=False, server_default="40"),
            sa.Column("created_by", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error", sa.Text, nullable=True),
        )
        op.create_index("ix_outbound_job_status", "outbound_job", ["status"])

    if "outbound_job_recipient" not in existing_tables:
        op.create_table(
            "outbound_job_recipient",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("job_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("outbound_job.id", ondelete="CASCADE"), nullable=False),
            sa.Column("position", sa.Integer, nullable=False, server_default="0"),
            sa.Column("phone", sa.String(320), nullable=False),
            sa.Column("variables", postgresql.JSONB, nullable=False,
                      server_default=sa.text("'[]'::jsonb")),
            sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
            sa.Column("external_id", sa.String(255), nullable=True),
            sa.Column("error", sa.Text, nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_outbound_job_recipient_job_id", "outbound_job_recipient", ["job_id"]
        )
        op.create_index(
            "ix_outbound_job_recipient_status", "outbound_job_recipient", ["status"]
        )


def downgrade() -> None:
    op.drop_index("ix_outbound_job_recipient_status", "outbound_job_recipient")
    op.drop_index("ix_outbound_job_recipient_job_id", "outbound_job_recipient")
    op.drop_table("outbound_job_recipient")

    op.drop_index("ix_outbound_job_status", "outbound_job")
    op.drop_table("outbound_job")
