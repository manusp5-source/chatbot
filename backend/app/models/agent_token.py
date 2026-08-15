"""Tokens de agente: credenciales para agentes EXTERNOS del operador que
consumen /agent-api/v1.

Seguridad:
  - El token en claro (`<prefijo>_<random>`) se muestra UNA vez al crearlo; aquí
    solo se guarda su SHA-256 (`token_hash`) — un volcado de la BD no revela
    tokens utilizables. `token_prefix` (primeros caracteres) es solo para que
    el admin reconozca el token en la lista.
  - `scopes` acota lo que puede hacer (CSV): monitor:read, interactions:read,
    kb:read, kb:write, prompts:write. Sin ámbito, sin acceso.
  - Revocable (active=False) y con caducidad opcional. `last_used_at` permite
    detectar uso inesperado.
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

# Ámbitos válidos (CSV en `scopes`).
#   - monitor:read      → métricas agregadas + salud operativa (costes LLM,
#     estado de canales, health). NO existe un `health:read` aparte: la salud
#     operativa es la misma clase de riesgo que las métricas agregadas (cero
#     PII, cero contenido) y partirla en dos ámbitos solo fragmentaría tokens.
#   - interactions:read → metadata de conversaciones (id, canal, estado,
#     tiempos). Ámbito propio porque, aunque sin contenido ni PII, expone
#     actividad por conversación individual (contact solo como UUID).
AGENT_TOKEN_SCOPES = {"monitor:read", "interactions:read", "kb:read", "kb:write", "prompts:write"}


class AgentToken(Base):
    __tablename__ = "agent_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    scopes: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def scope_set(self) -> set[str]:
        return {s.strip() for s in (self.scopes or "").split(",") if s.strip()}
