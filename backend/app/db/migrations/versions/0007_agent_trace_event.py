"""create agent_trace_event

Revision ID: 0007_agent_trace_event
Revises: 0006_conversation_archived
Create Date: 2026-05-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "0007_agent_trace_event"
down_revision = "0006_conversation_archived"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IMPORTANTE: usamos postgresql.ENUM con create_type=False y creamos
    # los tipos manualmente con IF NOT EXISTS. La razón:
    #   - sa.Enum(..., name=...) le pide a SQLAlchemy que cree el tipo
    #     al primer encuentro. Cuando el tipo aparece como Column en
    #     create_table, SA intenta crearlo otra vez sin checkfirst →
    #     "type ... already exists" si la creación previa lo dejó persistido.
    #   - Hacer el CREATE TYPE manualmente con DO/EXCEPTION lo hace
    #     idempotente: si ya existía (por una migración a medias previa),
    #     no rompe.
    op.execute(
        "DO $$ BEGIN "
        "CREATE TYPE trace_event_type AS ENUM "
        "('llm_call','tool_invocation','kb_lookup','router_decision','error'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "CREATE TYPE trace_level AS ENUM ('info','warn','error'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
    )

    trace_event_type = postgresql.ENUM(
        "llm_call", "tool_invocation", "kb_lookup", "router_decision", "error",
        name="trace_event_type", create_type=False,
    )
    trace_level = postgresql.ENUM(
        "info", "warn", "error",
        name="trace_level", create_type=False,
    )

    # Idempotente por si una corrida previa creó la tabla a medias
    bind = op.get_bind()
    if "agent_trace_event" not in inspect(bind).get_table_names():
        op.create_table(
            "agent_trace_event",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "conversation_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "message_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("messages.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("event_type", trace_event_type, nullable=False),
            sa.Column(
                "level",
                trace_level,
                nullable=False,
                server_default=sa.text("'info'"),
            ),
            sa.Column(
                "payload",
                postgresql.JSONB,
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column("latency_ms", sa.Integer, nullable=True),
            sa.Column("summary", sa.String(500), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_agent_trace_event_created_at", "agent_trace_event", ["created_at"]
        )
        op.create_index(
            "ix_agent_trace_conv_created",
            "agent_trace_event",
            ["conversation_id", "created_at"],
        )
        op.create_index("ix_agent_trace_type", "agent_trace_event", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_agent_trace_type", "agent_trace_event")
    op.drop_index("ix_agent_trace_conv_created", "agent_trace_event")
    op.drop_index("ix_agent_trace_event_created_at", "agent_trace_event")
    op.drop_table("agent_trace_event")
    sa.Enum(name="trace_level").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="trace_event_type").drop(op.get_bind(), checkfirst=True)
