"""Baja permanente de difusiones (opt-out).

Por qué una tabla y no la blocklist de Redis: la blocklist (`guard:blocked:*`)
CADUCA — 24 h el bloqueo automático, 30 días el manual — y vive en un Redis que
puede reiniciarse sin persistencia. Quien pide la baja hoy volvía a entrar en la
campaña de dentro de dos meses, o mañana si Redis se reiniciaba. Una baja es
para siempre hasta que la persona diga lo contrario: eso es una fila en
Postgres, no una clave con TTL.

La blocklist sigue usándose para lo suyo (abuso, cortes temporales); el opt-out
es otra cosa y se consulta ADEMÁS, en toda difusión.
"""
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base

if TYPE_CHECKING:
    from app.models.user import User


# De dónde salió la baja. Sirve para explicarlo en la ficha del contacto.
OPTOUT_SOURCE_MANUAL = "manual"      # la dio de baja una operadora
OPTOUT_SOURCE_REPLY = "reply"        # el cliente contestó BAJA / STOP
OPTOUT_SOURCE_IMPORT = "import"      # venía de una lista importada


class OutboundOptOut(Base):
    """Un destinatario que NO debe recibir difusiones, nunca más.

    `phone` guarda el MISMO identificador que usa el envío: teléfono E.164
    normalizado o `wa:<bsuid>` cuando el cliente no tiene teléfono visible. Se
    normaliza siempre al escribir con `_normalize_phone`, para que las tres
    formas de escribir un número (`+34600…`, `34600…`, `0034600…`) sean la
    misma baja y no tres filas distintas.
    """

    __tablename__ = "outbound_optout"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default=OPTOUT_SOURCE_MANUAL)
    # Texto libre: el mensaje del cliente que disparó la baja, o la nota de la
    # operadora. Lo lee un humano en la ficha del contacto.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    creator: Mapped["User | None"] = relationship()


# Palabras con las que la gente pide la baja de verdad. La comparación es sobre
# el mensaje ENTERO normalizado (sin acentos, signos ni mayúsculas): quien
# escribe "stop" pide la baja, quien escribe "no me pares el pedido" no.
OPTOUT_KEYWORDS: frozenset[str] = frozenset(
    {
        "baja",
        "darme de baja",
        "date de baja",
        "dar de baja",
        "me doy de baja",
        "quiero darme de baja",
        "stop",
        "unsubscribe",
        "cancelar suscripcion",
        "no quiero mas mensajes",
        "no me escribas mas",
        "dejad de escribirme",
        "dejen de escribirme",
    }
)


def is_optout_request(texto: str | None) -> bool:
    """¿Este mensaje entrante es una petición de baja? Función pura.

    Solo dispara con el mensaje COMPLETO: buscar "baja" en cualquier sitio daría
    de baja a quien escriba "me han dado la baja médica". Un mensaje de baja es
    corto y va solo.
    """
    import re
    import unicodedata

    if not texto:
        return False
    limpio = unicodedata.normalize("NFKD", texto.strip().lower())
    limpio = "".join(c for c in limpio if not unicodedata.combining(c))
    limpio = re.sub(r"[^a-z0-9\s]", " ", limpio)
    limpio = re.sub(r"\s+", " ", limpio).strip()
    return limpio in OPTOUT_KEYWORDS


async def add_outbound_optout(
    db,
    phone: str,
    *,
    source: str = OPTOUT_SOURCE_MANUAL,
    reason: str | None = None,
    created_by: uuid.UUID | None = None,
) -> bool:
    """Da de baja un destinatario. Devuelve False si ya lo estaba. NO hace commit.

    Idempotente a propósito: quien conteste BAJA tres veces no genera tres filas
    ni un error. Normaliza el identificador con la MISMA función que el envío,
    para que las tres formas de escribir un número sean una sola baja.
    """
    from sqlalchemy import select

    from app.providers.whatsapp.ycloud import _normalize_phone

    normalized = _normalize_phone((phone or "").strip())
    if not normalized:
        raise ValueError("Teléfono vacío")
    existing = (
        await db.execute(
            select(OutboundOptOut).where(OutboundOptOut.phone == normalized)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False
    db.add(
        OutboundOptOut(
            phone=normalized,
            source=source,
            reason=(reason or None)[:1000] if reason else None,
            created_by=created_by,
        )
    )
    return True


async def optout_if_requested(db, phone: str, texto: str | None) -> bool:
    """Si el mensaje entrante pide la baja, la registra. Devuelve si la registró.

    Punto de enganche para el inbound (`services/conversation.py`): una sola
    línea, sin commit propio, para que entre en la misma transacción del mensaje.
    """
    if not is_optout_request(texto):
        return False
    return await add_outbound_optout(
        db, phone, source=OPTOUT_SOURCE_REPLY, reason=(texto or "")[:200]
    )
