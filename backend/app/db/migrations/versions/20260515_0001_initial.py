"""Esquema inicial completo

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-15

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Extensiones
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')

    # Enums
    op.execute("CREATE TYPE user_role AS ENUM ('admin', 'cliente')")
    op.execute(
        "CREATE TYPE contact_estado AS ENUM "
        "('contacto', 'solicitud_presupuesto', 'seguimiento', 'cliente', 'perdido', 'no_cualifica')"
    )
    op.execute("CREATE TYPE contact_origen AS ENUM ('whatsapp', 'web', 'manual')")
    op.execute("CREATE TYPE conversation_status AS ENUM ('bot', 'humano', 'cerrada')")
    op.execute("CREATE TYPE conversation_canal AS ENUM ('whatsapp', 'web')")
    op.execute("CREATE TYPE message_role AS ENUM ('user', 'assistant', 'operator', 'system')")
    op.execute("CREATE TYPE document_formato AS ENUM ('pdf', 'docx', 'txt', 'md', 'csv', 'xlsx')")
    op.execute("CREATE TYPE document_status AS ENUM ('procesando', 'indexado', 'error')")

    # users
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", postgresql.ENUM("admin", "cliente", name="user_role", create_type=False), nullable=False),
        sa.Column("nombre", sa.String(120)),
        sa.Column("activo", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"])

    # credentials
    op.create_table(
        "credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("key", sa.String(80), unique=True, nullable=False),
        sa.Column("value_encrypted", sa.LargeBinary, nullable=False),
        sa.Column("descripcion", sa.Text),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
    )

    # agent_config
    op.create_table(
        "agent_config",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("prompt_system", sa.Text, nullable=False),
        sa.Column("model_name", sa.String(80), nullable=False, server_default="gpt-5.4-mini"),
        sa.Column("temperature", sa.Numeric(3, 2), nullable=False, server_default="0.3"),
        sa.Column("max_tokens", sa.Integer, nullable=False, server_default="1024"),
        sa.Column("buffer_seconds", sa.Integer, nullable=False, server_default="5"),
        sa.Column("response_split_max_parts", sa.Integer, nullable=False, server_default="3"),
        sa.Column("context_window", sa.Integer, nullable=False, server_default="20"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
    )
    op.create_index("ix_agent_config_is_active", "agent_config", ["is_active"])

    # contacts
    op.create_table(
        "contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("telefono", sa.String(40), unique=True, nullable=False),
        sa.Column("email", sa.String(255)),
        sa.Column("nombre", sa.String(120)),
        sa.Column(
            "estado",
            postgresql.ENUM(
                "contacto", "solicitud_presupuesto", "seguimiento", "cliente", "perdido", "no_cualifica",
                name="contact_estado", create_type=False,
            ),
            nullable=False, server_default="contacto",
        ),
        sa.Column(
            "origen",
            postgresql.ENUM("whatsapp", "web", "manual", name="contact_origen", create_type=False),
            nullable=False,
        ),
        sa.Column("servicio_interes", sa.String(200)),
        sa.Column("ultimo_mensaje_at", sa.DateTime(timezone=True)),
        sa.Column("notas_internas", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_contacts_telefono", "contacts", ["telefono"])
    op.create_index("ix_contacts_estado", "contacts", ["estado"])
    op.create_index("ix_contacts_ultimo_mensaje_at", "contacts", ["ultimo_mensaje_at"])

    # tags
    op.create_table(
        "tags",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("nombre", sa.String(60), unique=True, nullable=False),
        sa.Column("color", sa.String(7), nullable=False, server_default="#3b82f6"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # contact_tags
    op.create_table(
        "contact_tags",
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tag_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # conversations
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "canal",
            postgresql.ENUM("whatsapp", "web", name="conversation_canal", create_type=False),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(80), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM("bot", "humano", "cerrada", name="conversation_status", create_type=False),
            nullable=False, server_default="bot",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        sa.Column("resumen", sa.Text),
        sa.Column("derivada_a_humano_at", sa.DateTime(timezone=True)),
        sa.Column("asignada_a", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
    )
    op.create_index("ix_conversations_contact_id", "conversations", ["contact_id"])
    op.create_index("ix_conversations_status", "conversations", ["status"])
    op.create_index("ix_conversations_last_message_at", "conversations", ["last_message_at"])

    # messages
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "rol",
            postgresql.ENUM("user", "assistant", "operator", "system", name="message_role", create_type=False),
            nullable=False,
        ),
        sa.Column("contenido", sa.Text),
        sa.Column("audio_url", sa.String(500)),
        sa.Column("audio_transcript", sa.Text),
        sa.Column("metadata", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("leido_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index("ix_messages_conv_created", "messages", ["conversation_id", "created_at"])

    # documents
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("nombre", sa.String(255), nullable=False),
        sa.Column(
            "formato",
            postgresql.ENUM("pdf", "docx", "txt", "md", "csv", "xlsx", name="document_formato", create_type=False),
            nullable=False,
        ),
        sa.Column("tamano_bytes", sa.Integer, nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM("procesando", "indexado", "error", name="document_status", create_type=False),
            nullable=False, server_default="procesando",
        ),
        sa.Column("error_msg", sa.Text),
        sa.Column("num_chunks", sa.Integer, nullable=False, server_default="0"),
        sa.Column("storage_path", sa.String(500)),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
    )

    # chunks (con vector(1536))
    op.execute(
        """
        CREATE TABLE chunks (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            contenido TEXT NOT NULL,
            embedding vector(1536) NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.execute(
        "CREATE INDEX ix_chunks_embedding ON chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )

    # audit_log
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("entity", sa.String(40), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True)),
        sa.Column("before", postgresql.JSONB),
        sa.Column("after", postgresql.JSONB),
        sa.Column("ip", sa.String(45)),
        sa.Column("user_agent", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_log_user_id", "audit_log", ["user_id"])
    op.create_index("ix_audit_log_entity_entity_id", "audit_log", ["entity", "entity_id"])

    # Función match_chunks para búsqueda RAG
    op.execute(
        """
        CREATE OR REPLACE FUNCTION match_chunks(
          query_embedding vector(1536),
          match_threshold float DEFAULT 0.5,
          match_count int DEFAULT 5
        )
        RETURNS TABLE (
          id uuid,
          document_id uuid,
          contenido text,
          metadata jsonb,
          similarity float
        )
        LANGUAGE sql STABLE
        AS $$
          SELECT
            chunks.id,
            chunks.document_id,
            chunks.contenido,
            chunks.metadata,
            1 - (chunks.embedding <=> query_embedding) AS similarity
          FROM chunks
          WHERE 1 - (chunks.embedding <=> query_embedding) > match_threshold
          ORDER BY chunks.embedding <=> query_embedding
          LIMIT match_count;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS match_chunks(vector, float, int)")
    op.drop_table("audit_log")
    op.execute("DROP TABLE IF EXISTS chunks")
    op.drop_table("documents")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("contact_tags")
    op.drop_table("tags")
    op.drop_table("contacts")
    op.drop_table("agent_config")
    op.drop_table("credentials")
    op.drop_table("users")
    for enum_name in [
        "document_status", "document_formato", "message_role", "conversation_canal",
        "conversation_status", "contact_origen", "contact_estado", "user_role",
    ]:
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
