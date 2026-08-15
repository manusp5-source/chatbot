"""create agents + channels + external_apis (multichannel, Fase 1)

Revision ID: 0009_multichannel_tables
Revises: 0008_message_media_columns
Create Date: 2026-05-18

Migración SOFT: las tablas nuevas se crean y se llenan con datos derivados
de `agent_config` + `credentials`, pero el runtime sigue leyendo de las
tablas viejas. F2 y F3 cambiarán la lectura a las tablas nuevas, y solo
entonces (en una migración posterior) se retirarán las viejas.

Data seeding:
- Si hay un AgentConfig activo → se crea un Agent "Agente por defecto".
- Si hay credenciales YCloud → se crea un Channel WhatsApp asociado a ese
  Agent, con las credenciales WA serializadas como JSON cifrado.
- Si hay credenciales OpenAI/Resend/Telegram/Slack → se crea una entrada
  ExternalAPI por cada provider con la credencial cifrada.
"""
from alembic import op
import json
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "0009_multichannel_tables"
down_revision = "0008_message_media_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------- Enums (idempotente, mismo patrón que 0007) ----------
    op.execute(
        "DO $$ BEGIN "
        "CREATE TYPE channel_type AS ENUM "
        "('whatsapp','webchat','retell_voice','instagram_dm'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
    )
    channel_type = postgresql.ENUM(
        "whatsapp", "webchat", "retell_voice", "instagram_dm",
        name="channel_type", create_type=False,
    )

    bind = op.get_bind()
    existing_tables = set(inspect(bind).get_table_names())

    # ---------- agents ----------
    if "agents" not in existing_tables:
        op.create_table(
            "agents",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("prompt_system", sa.Text, nullable=False),
            sa.Column("model_name", sa.String(80), nullable=False, server_default="gpt-5.4-mini"),
            sa.Column("temperature", sa.Numeric(3, 2), nullable=False, server_default="1.0"),
            sa.Column("max_tokens", sa.Integer, nullable=False, server_default="0"),
            sa.Column("buffer_seconds", sa.Integer, nullable=False, server_default="5"),
            sa.Column("response_split_max_parts", sa.Integer, nullable=False, server_default="3"),
            sa.Column("context_window", sa.Integer, nullable=False, server_default="20"),
            sa.Column("handoff_bridge_message", sa.Text, nullable=True),
            sa.Column("monthly_budget_usd", sa.Numeric(10, 2), nullable=True),
            sa.Column("tools_enabled", postgresql.JSONB, nullable=True),
            sa.Column("knowledge_base_filter", postgresql.JSONB, nullable=True),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("created_by", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        )
        op.create_index("ix_agents_is_active", "agents", ["is_active"])

    # ---------- channels ----------
    if "channels" not in existing_tables:
        op.create_table(
            "channels",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("type", channel_type, nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
            sa.Column("credentials_encrypted", sa.LargeBinary, nullable=True),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
            sa.Column("config", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_channels_enabled", "channels", ["enabled"])
        op.create_index("ix_channels_agent_id", "channels", ["agent_id"])

    # ---------- external_apis ----------
    if "external_apis" not in existing_tables:
        op.create_table(
            "external_apis",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("provider", sa.String(40), nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("credentials_encrypted", sa.LargeBinary, nullable=True),
            sa.Column("extra", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("provider", "name", name="uq_external_apis_provider_name"),
        )
        op.create_index("ix_external_apis_provider", "external_apis", ["provider"])
        op.create_index("ix_external_apis_is_active", "external_apis", ["is_active"])

    # ---------- Data seeding ----------
    # Se hace dentro de un SAVEPOINT (begin_nested) para que un error en el
    # seed NO aborte la transacción principal de la migración. Si Postgres
    # entra en estado "aborted", el siguiente UPDATE alembic_version falla y
    # el container muere en bucle. Con savepoint, si falla el seed solo se
    # revierte el bloque del seed; la migración (CREATE TABLE) ya está y
    # alembic puede actualizar la version_num normalmente.
    savepoint = bind.begin_nested()
    try:
        _seed_from_legacy()
        savepoint.commit()
    except Exception as e:
        savepoint.rollback()
        print(f"[0009 seed] aviso: no se pudo migrar legacy data: {e}")


def downgrade() -> None:
    op.drop_index("ix_external_apis_is_active", "external_apis")
    op.drop_index("ix_external_apis_provider", "external_apis")
    op.drop_table("external_apis")

    op.drop_index("ix_channels_agent_id", "channels")
    op.drop_index("ix_channels_enabled", "channels")
    op.drop_table("channels")

    op.drop_index("ix_agents_is_active", "agents")
    op.drop_table("agents")

    sa.Enum(name="channel_type").drop(op.get_bind(), checkfirst=True)


# ---------- Helpers de seeding ----------


def _seed_from_legacy() -> None:
    """Copia AgentConfig activo → Agent, y credentials sueltas → Channel/ExternalAPI.

    Se ejecuta dentro del mismo `op.get_bind()` para que comparta la
    transacción del upgrade.
    """
    bind = op.get_bind()

    # 1) AgentConfig activo → Agent "Agente por defecto"
    res = bind.execute(sa.text(
        "SELECT id, prompt_system, model_name, temperature, max_tokens, "
        "buffer_seconds, response_split_max_parts, context_window, "
        "handoff_bridge_message, monthly_budget_usd "
        "FROM agent_config WHERE is_active = true LIMIT 1"
    )).first()

    new_agent_id = None
    if res:
        new_agent_id_row = bind.execute(sa.text(
            "INSERT INTO agents (name, prompt_system, model_name, temperature, "
            "max_tokens, buffer_seconds, response_split_max_parts, context_window, "
            "handoff_bridge_message, monthly_budget_usd, is_active) "
            "VALUES (:name, :prompt, :model, :temp, :max_t, :buf, :split, :ctx, "
            ":handoff, :budget, true) RETURNING id"
        ), {
            "name": "Agente por defecto",
            "prompt": res.prompt_system,
            "model": res.model_name,
            "temp": float(res.temperature) if res.temperature is not None else 1.0,
            "max_t": res.max_tokens or 0,
            "buf": res.buffer_seconds or 5,
            "split": res.response_split_max_parts or 3,
            "ctx": res.context_window or 20,
            "handoff": res.handoff_bridge_message,
            "budget": float(res.monthly_budget_usd) if res.monthly_budget_usd is not None else None,
        })
        new_agent_id = new_agent_id_row.scalar()

    # 2) Credenciales por proveedor — se leen como están (cifradas) y se
    # rebobinan a las tablas nuevas. Los blobs ya están cifrados con el
    # ENCRYPTION_KEY del sistema, no los desciframos aquí; los rempaquetamos.
    #
    # Para WhatsApp: ycloud_api_key + ycloud_webhook_secret + ycloud_phone_number
    # se serializan en un único JSON y se vuelven a cifrar — pero como no
    # tenemos acceso al cifrador en la migración (vive en Python con env),
    # almacenamos los blobs originales tal cual en un JSONB plain.
    # Decisión: en `channels.credentials_encrypted` guardamos un BLOB con un
    # JSON serializado de los blobs originales (b64) y el código de F2
    # tendrá un loader que sabe deserializarlos. Es feo pero conserva la
    # seguridad sin requerir que la migración tenga la KEY.
    #
    # Alternativa más simple para v1: dejar `channels` sin credenciales
    # (NULL) y que el código siga leyendo de `credentials` por su key.
    # F2 añadirá la UI para que el usuario re-introduzca/copy las creds.
    #
    # Voy con la alternativa simple → channels.credentials_encrypted = NULL
    # y un Channel WhatsApp default queda creado para asociar runtime.

    # Solo creamos el Channel WhatsApp si HAY credenciales YCloud configuradas
    # (no tiene sentido un canal vacío). Usamos CAST(...AS jsonb) en vez de
    # `:placeholder::jsonb` para evitar la confusión de psycopg2 entre el
    # doble dos puntos del cast y el sufijo del parámetro nombrado.
    has_ycloud = bind.execute(sa.text(
        "SELECT 1 FROM credentials WHERE key = 'ycloud_api_key' LIMIT 1"
    )).first()
    if has_ycloud:
        bind.execute(sa.text(
            "INSERT INTO channels (type, name, enabled, agent_id, config) "
            "VALUES ('whatsapp', :name, true, :agent_id, CAST(:config AS jsonb))"
        ), {
            "name": "WhatsApp",
            "agent_id": new_agent_id,
            "config": json.dumps({
                "legacy_credentials_keys": [
                    "ycloud_api_key", "ycloud_webhook_secret", "ycloud_phone_number"
                ],
                "migrated_from_legacy": True,
            }),
        })

    # 3) ExternalAPIs — una entrada por provider que tenga creds activas en
    # la tabla legacy `credentials`. Solo marca la existencia, no copia el
    # blob (F2 leerá la UI y el usuario podrá editar/rotar).
    provider_keys = {
        "openai": ["openai_api_key"],
        "resend": ["resend_api_key", "resend_from_email"],
        "telegram": ["telegram_bot_token", "telegram_chat_id"],
        "slack": ["slack_webhook_url"],
    }
    for provider, keys in provider_keys.items():
        has_any = bind.execute(sa.text(
            "SELECT 1 FROM credentials WHERE key = ANY(:keys) LIMIT 1"
        ), {"keys": keys}).first()
        if has_any:
            bind.execute(sa.text(
                "INSERT INTO external_apis (provider, name, extra, is_active) "
                "VALUES (:provider, :name, CAST(:extra AS jsonb), true) "
                "ON CONFLICT (provider, name) DO NOTHING"
            ), {
                "provider": provider,
                "name": provider,
                "extra": json.dumps({
                    "legacy_credentials_keys": keys,
                    "migrated_from_legacy": True,
                }),
            })
