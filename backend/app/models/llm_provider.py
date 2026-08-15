"""Modelo LLMProvider — proveedores de LLM configurables desde el panel.

Generaliza los 2 slots fijos anteriores (OpenAI primario + fallback por claves
de credenciales hardcodeadas) a N proveedores compatibles con la API de OpenAI
(OpenAI, OpenRouter, DeepSeek, Z.AI, Together, Azure…). Cada agente (WhatsApp,
Instagram, voz, clasificador, agente interno) puede apuntar a uno distinto y
elegir el modelo por texto libre — sin tocar código al salir un modelo nuevo.

La `api_key` se guarda CIFRADA (Fernet, igual que el resto de credenciales).
`base_url` NULL = OpenAI oficial. `api_key` vacía en el proveedor sembrado
"OpenAI" → cae a la credencial `openai_api_key` existente (retrocompatibilidad:
no hay que reintroducir la clave al desplegar esta versión).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encrypted_type import EncryptedText
from app.db.session import Base


class LLMProvider(Base):
    __tablename__ = "llm_providers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    # NULL / vacío = API oficial de OpenAI. Cualquier gateway compatible OpenAI
    # va aquí (https://openrouter.ai/api/v1, https://api.deepseek.com, …).
    base_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Clave cifrada. Vacía solo en el proveedor "OpenAI" sembrado → usa la
    # credencial openai_api_key existente.
    api_key: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    # Muchos gateways/modelos (DeepSeek, la mayoría de OpenRouter) SÍ aceptan
    # temperature/max_tokens. Los "reasoning" de OpenAI (gpt-5/o1/o3/o4) NO.
    # Este flag sustituye a la heurística por nombre de modelo, que fallaba con
    # modelos no-OpenAI de nombre parecido.
    accepts_temperature: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Proveedor por defecto: lo usan los agentes sin proveedor asignado y las
    # tareas de sistema (aprendizaje). Solo uno debería tenerlo a True.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Respaldo GLOBAL: si el proveedor primario de cualquier agente falla, la
    # llamada se reintenta contra este (con fallback_model). Solo uno a True.
    # Por agente se puede sobreescribir (agents.fallback_provider_id).
    is_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Modelo usado cuando este proveedor actúa de respaldo (los ids del
    # primario no existen en otro gateway). Solo aplica con is_fallback=True
    # o cuando un agente lo referencia como fallback sin modelo propio.
    fallback_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
