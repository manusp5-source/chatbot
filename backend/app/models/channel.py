"""Modelo Channel (canales de entrada, Fase 1).

Representa una conexión a un canal de mensajería (WhatsApp, widget web,
Retell voz, Instagram DM). En F1 es estructural (solo se almacena), en
F2 se podrá editar desde la UI Conexiones.

Cada channel apunta a UN agent (relación 1:1 channel→agent en v1, según
decisión del administrador). Tiene sus propias credentials cifradas (token YCloud,
secret webhook, account_sid Retell, etc) en lugar de la tabla global
`credentials`.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, LargeBinary, String, func
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ChannelType(str, enum.Enum):
    whatsapp = "whatsapp"
    webchat = "webchat"
    retell_voice = "retell_voice"
    instagram_dm = "instagram_dm"
    email = "email"


channel_type_enum = ENUM(
    ChannelType,
    name="channel_type",
    create_type=False,
    values_callable=lambda x: [e.value for e in x],
)


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[ChannelType] = mapped_column(channel_type_enum, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    # Secretos del canal: JSON cifrado con Fernet (igual que
    # `credentials.value_encrypted`). Qué guarda cada tipo lo manda
    # `app/services/channel_secrets.SECRET_KEYS` — hoy:
    #   instagram_dm = {app_secret, page_access_token, verify_token}
    #   retell_voice = {api_key, webhook_secret (legado, ya no se pide)}
    # WhatsApp no usa esta columna: sus credenciales viven en la tabla global
    # `credentials`, ya cifrada.
    #
    # NO leas ni escribas esto a mano: `channel_secrets.channel_config` para
    # leer (junta config + secretos, con respaldo en plano para instalaciones
    # anteriores a la migración 0054) y `apply_channel_values` para escribir.
    #
    # OJO con webchat: su api_key NO es un secreto de servidor, viaja en el
    # HTML público de la web del cliente (`<script data-api-key=...>`), así que
    # vive en `config` (plano) y no aquí. Ver `config` abajo.
    credentials_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Config visible por canal (greeting, horario, máx caracteres, etc).
    #
    # Webchat usa además:
    #   - `api_key` (str): la clave pública del widget. La genera/regenera el
    #     panel. Su huella va dentro del session_token, así que regenerarla
    #     invalida de verdad todas las sesiones ya emitidas.
    #   - `allowed_domains` (list[str]): dominios donde se acepta el widget.
    #     Vacío o ausente = sin restricción (instalaciones anteriores). Se
    #     admite `ejemplo.com` (exacto) y `*.ejemplo.com` (con subdominios).
    #     Lo valida `app/api/webchat.py` contra la cabecera Origin (o Referer)
    #     en `POST /webchat/sessions` y `POST /webchat/messages`. Es el único
    #     freno real contra copiar el snippet a otra web y gastar presupuesto
    #     de modelo: la api_key es pública por diseño.
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
