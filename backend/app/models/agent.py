"""Modelo Agent (multi-agente, Fase 1).

Reemplaza progresivamente al singleton `agent_config`. La diferencia clave:
puede haber varios agents activos a la vez (uno por canal, por ejemplo
"Ventas" en widget web y "Soporte" en WhatsApp), todos sobre la misma
base de conocimiento pero con prompts y modelos distintos.

Durante la fase de migración soft, el código de runtime sigue leyendo
`agent_config`. Esta tabla se llena con un agent "default" derivado de
`agent_config` para que F2 y F3 puedan cambiar la lectura sin downtime.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Naturaleza del agente: 'text' (WhatsApp, webchat, Instagram, email) o
    # 'voice' (llamadas Retell). NO es cosmético: el runtime lo usa para que un
    # agente de llamadas no acabe atendiendo un chat ni al revés. Un prompt de
    # voz ("atiendes llamadas telefónicas", sin derivar a humano, respuesta en un
    # bloque) es inservible en WhatsApp, y un prompt de chat (markdown, enlaces,
    # viñetas) es impronunciable en una llamada.
    kind: Mapped[str] = mapped_column(
        String(10), nullable=False, default="text", server_default="text", index=True
    )
    prompt_system: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(String(80), nullable=False, default="gpt-5.4-mini")
    # Proveedor LLM de este agente. NULL = proveedor por defecto (retrocompat).
    llm_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("llm_providers.id", ondelete="SET NULL"), nullable=True
    )
    # Respaldo POR AGENTE (opcional): si su primario falla, reintenta contra
    # este proveedor con fallback_model. NULL → aplica el respaldo global
    # (llm_providers.is_fallback) y, si tampoco, las credenciales legacy.
    fallback_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("llm_providers.id", ondelete="SET NULL"), nullable=True
    )
    fallback_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    temperature: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False, default=1.0)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    buffer_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    response_split_max_parts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    context_window: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    handoff_bridge_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    monthly_budget_usd: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    # Lista de tools habilitados para este agente. Si NULL → todos disponibles.
    # Formato: ["consultar_kb", "derivar_humano", "guardar_contacto", ...]
    tools_enabled: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # Filtro opcional de KB: solo chunks con estas categorías. NULL = sin filtro.
    knowledge_base_filter: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
