"""Configuracion del Agente Interno (singleton).

A diferencia de `agent_config` (con versionado e is_active para el agente
publico que habla con clientes), aqui guardamos una unica fila editable
in-place. El agente interno es solo lectura: las pruebas de regresion las
hace el propio operador al usarlo, no necesitamos historial de prompts.

Si la fila no existe, el endpoint la crea en su primera lectura con los
defaults del seed de la migracion 0012.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class InternalAgentConfig(Base):
    __tablename__ = "internal_agent_config"
    __table_args__ = (
        # Forzamos singleton: solo se permite un id concreto.
        CheckConstraint(
            "id = '00000000-0000-0000-0000-000000000001'",
            name="ck_internal_agent_config_singleton",
        ),
    )

    SINGLETON_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    model_name: Mapped[str] = mapped_column(String(80), nullable=False, default="gpt-5.4-mini")
    llm_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("llm_providers.id", ondelete="SET NULL"), nullable=True
    )
    temperature: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False, default=0.2)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=1500)
    monthly_budget_usd: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    # Tope de queries por usuario admin por dia (rate limit en Redis).
    daily_query_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
