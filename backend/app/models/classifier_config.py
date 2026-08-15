import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

# Singleton: una sola fila con este id fijo (igual que internal_agent_config).
SINGLETON_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class ClassifierConfig(Base):
    """Config del agente clasificador pre-bot (anti-spam). Singleton.

    `enabled=False` por defecto: hasta que un admin lo active, no se clasifica
    nada (el bot funciona igual que siempre).
    """

    __tablename__ = "classifier_config"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Lista de canales donde aplica, p.ej. ["instagram_dm", "whatsapp", "web"].
    channels: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(String(80), nullable=False, default="gpt-5.4-mini")
    llm_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("llm_providers.id", ondelete="SET NULL"), nullable=True
    )
    temperature: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
