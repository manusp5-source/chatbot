"""Modelo ExternalAPI (Fase 1).

Conexiones a servicios externos que el agente puede usar como tools:
OpenAI (LLM + embeddings + moderation), Google Calendar, Google Gmail,
Retell AI, etc.

Cada producto Google es una conexión independiente (decisión del administrador):
"google_calendar" y "google_gmail" son providers distintos. Así el
cliente puede revocar uno sin tirar el otro.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, LargeBinary, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ExternalAPI(Base):
    __tablename__ = "external_apis"
    __table_args__ = (UniqueConstraint("provider", "name", name="uq_external_apis_provider_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # provider canónico: openai | google_calendar | google_gmail | google_drive |
    # retell | anthropic | (más en el futuro). Cada uno tiene formato de
    # credentials distinto.
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # Label legible. Por defecto = provider, pero permite múltiples cuentas del
    # mismo provider (ej. "OpenAI principal" vs "OpenAI dev").
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    credentials_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # Metadata adicional no sensible (scopes OAuth concedidos, fecha de
    # último uso, etc).
    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
