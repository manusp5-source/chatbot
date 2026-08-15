import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encrypted_type import EncryptedText
from app.db.session import Base


class LearnedRule(Base):
    """Autoaprendizaje · Fase 2 — reglas de estilo aprendidas.

    Cuando la operadora corrige varias veces lo mismo sobre el TONO/ESTILO del
    agente ("sé más cálida", "no ofrezcas descuentos"), la pantalla "Aprendizajes"
    propone una regla de estilo. Al aprobarla se guarda aquí y el agente la lee
    en cada turno: se inyecta un bloque "Reglas aprendidas (estilo)" en el prompt
    de sistema de TODOS los agentes conversacionales (negocio único → reglas
    globales). Nada se aprende solo: cada regla la aprueba una persona.

    A diferencia de una Q&A (que va a la base de conocimiento como `Document`),
    una regla de estilo no es un dato que el agente "busque", sino una pauta
    permanente de cómo responder; por eso vive aparte y se inyecta en el prompt.

    El texto va cifrado en reposo (EncryptedText → BYTEA), igual que el resto de
    PII/contenido del sistema.
    """

    __tablename__ = "learned_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # La regla en imperativo, concisa (1-2 líneas). Cifrada en reposo.
    text: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    # El hueco de conocimiento del que salió (trazabilidad). SET NULL: si se
    # purga el hueco, conservamos la regla activa.
    source_gap_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_gaps.id", ondelete="SET NULL"),
        nullable=True,
    )
    # La operadora puede desactivar una regla sin borrarla (deja de inyectarse).
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
