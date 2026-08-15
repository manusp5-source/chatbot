"""Precio por modelo LLM (USD por 1M de tokens), editable/actualizable.

Antes los precios vivían hardcodeados en `services/llm_pricing.py`. Ahora se
guardan aquí para poder:
- Auto-actualizarlos desde OpenRouter (tarea diaria + botón en el panel).
- Editarlos a mano cuando un modelo no está en OpenRouter o queremos ajustarlo.

La clave primaria es el NOMBRE del modelo tal cual lo usa la app (p. ej.
`gpt-5.4-mini`, `glm-5.2`). Así `estimate_cost_usd(model, ...)` puede buscar el
precio directamente por ese nombre.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class LLMModelPrice(Base):
    __tablename__ = "llm_model_price"

    # Nombre del modelo tal cual lo usa la app (el mismo string que se guarda en
    # llm_usage_log.model y se configura en los agentes).
    model: Mapped[str] = mapped_column(String(80), primary_key=True)
    # USD por 1M de tokens.
    input_per_1m: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    output_per_1m: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    # Origen del dato: 'seed' (tabla inicial), 'openrouter' (auto) o 'manual'.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="seed")
    # id del modelo en OpenRouter con el que casó (para ver/verificar el mapeo).
    openrouter_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
