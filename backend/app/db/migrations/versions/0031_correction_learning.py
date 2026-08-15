"""autoaprendizaje Fase 2 — aprender de correcciones repetidas

Revision ID: 0031_correction_learning
Revises: 0030_knowledge_gap
Create Date: 2026-06-17

Añade el soporte para el disparador "correcciones repetidas":

  - learned_rules: reglas de estilo aprobadas que se inyectan en el prompt del
    agente (negocio único → reglas globales). Texto cifrado en reposo (BYTEA).
  - knowledge_gaps.proposal_kind: distingue si aprobar el hueco crea una Q&A
    (Document, "content") o una regla de estilo (LearnedRule, "style").
  - agent_corrections.processed_at: marca de "ya analizada" por el detector
    periódico, para que cada corrida sea barata (solo embebe lo nuevo).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "0031_correction_learning"
down_revision = "0030_knowledge_gap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    # 1) Tabla learned_rules (idempotente).
    if "learned_rules" not in tables:
        op.create_table(
            "learned_rules",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            # Texto cifrado en reposo (Fernet) — tipo subyacente BYTEA.
            sa.Column("text", sa.LargeBinary(), nullable=False),
            sa.Column(
                "source_gap_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("knowledge_gaps.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "created_by",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index("ix_learned_rules_active", "learned_rules", ["active"])

    # 2) knowledge_gaps.proposal_kind (idempotente).
    if "knowledge_gaps" in tables:
        cols = {c["name"] for c in inspector.get_columns("knowledge_gaps")}
        if "proposal_kind" not in cols:
            op.add_column(
                "knowledge_gaps",
                sa.Column("proposal_kind", sa.String(length=20), nullable=True),
            )

    # 3) agent_corrections.processed_at (idempotente).
    if "agent_corrections" in tables:
        cols = {c["name"] for c in inspector.get_columns("agent_corrections")}
        if "processed_at" not in cols:
            op.add_column(
                "agent_corrections",
                sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
            )
            op.create_index(
                "ix_agent_corrections_processed_at",
                "agent_corrections",
                ["processed_at"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "agent_corrections" in tables:
        idx = {i["name"] for i in inspector.get_indexes("agent_corrections")}
        if "ix_agent_corrections_processed_at" in idx:
            op.drop_index(
                "ix_agent_corrections_processed_at", table_name="agent_corrections"
            )
        cols = {c["name"] for c in inspector.get_columns("agent_corrections")}
        if "processed_at" in cols:
            op.drop_column("agent_corrections", "processed_at")

    if "knowledge_gaps" in tables:
        cols = {c["name"] for c in inspector.get_columns("knowledge_gaps")}
        if "proposal_kind" in cols:
            op.drop_column("knowledge_gaps", "proposal_kind")

    if "learned_rules" in tables:
        idx = {i["name"] for i in inspector.get_indexes("learned_rules")}
        if "ix_learned_rules_active" in idx:
            op.drop_index("ix_learned_rules_active", table_name="learned_rules")
        op.drop_table("learned_rules")
