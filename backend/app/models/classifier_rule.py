"""Reglas duras del clasificador (filtro determinista, sin LLM).

El clasificador con IA interpreta; estas reglas NO. Son la lista de "esto no lo
quiero ver": un remitente, un dominio entero o un texto en el asunto. Se
evalúan ANTES de cualquier llamada al modelo, así que lo que caza no cuesta ni
un token, y el resultado es siempre el mismo (una regla no tiene un mal día).

Tres campos y una semántica fija por campo, para que en el panel no haya que
elegir "contiene / empieza por / es igual a":

  remitente  el email (o el @usuario) es EXACTAMENTE ese
  dominio    el email termina en @ese-dominio (cubre subdominios: mail.x.com)
  asunto     el asunto CONTIENE ese texto

Todo se compara en minúsculas y sin espacios sobrantes.
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

# Campos admitidos. Se validan en la API y se comprueban aquí con un CHECK en
# la tabla: una regla con un campo desconocido no filtraría nada y el fallo
# solo se vería en producción, sin ruido en los logs.
CAMPOS = ("remitente", "dominio", "asunto")


class ClassifierRule(Base):
    """Una regla dura. Lo que encaja va a 'Descartados' sin pasar por el bot."""

    __tablename__ = "classifier_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Canal al que aplica. NULL = todos los canales. El campo `asunto` solo
    # existe en email, así que una regla de asunto se ignora en el resto.
    canal: Mapped[str | None] = mapped_column(String(30))
    campo: Mapped[str] = mapped_column(String(20), nullable=False)
    valor: Mapped[str] = mapped_column(String(255), nullable=False)
    # Para qué la puso quien la puso. Dentro de seis meses, "@promo-x.com" sin
    # nota no le dice nada a nadie.
    nota: Mapped[str | None] = mapped_column(String(255))
    # Cuántas veces ha disparado. Una regla con 0 aciertos en meses sobra, y una
    # con miles puede estar comiéndose correo bueno: sin el contador no hay
    # forma de saberlo.
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
