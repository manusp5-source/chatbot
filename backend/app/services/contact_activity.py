"""Helper para registrar eventos en el timeline de actividad de un contacto.

Patrón: el registro se hace SIN commit propio. Se confía el commit a la acción
de API que lo invoca, para que el evento y la mutación viajen en la MISMA
transacción (atomicidad: si la acción falla y se hace rollback, no queda un
evento huérfano; si tiene éxito, el evento se persiste con ella).

Regla de oro: `meta` NUNCA debe llevar PII (ni teléfono, ni email, ni nombre del
contacto, ni texto de nota). Solo estados, nombre de etiqueta, canal e ids.
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact_activity import ContactActivity


async def record_activity(
    db: AsyncSession,
    contact_id: uuid.UUID,
    tipo: str,
    *,
    actor_user_id: uuid.UUID | None = None,
    meta: dict | None = None,
) -> None:
    """Añade un evento de actividad a la sesión (sin commit).

    Args:
        db: sesión async ya abierta por la acción llamante.
        contact_id: contacto al que pertenece el evento.
        tipo: clave del evento (contact_created, status_changed, tag_added,
            tag_removed, note_added, conversation_started).
        actor_user_id: usuario que ejecutó la acción; None = sistema/agente.
        meta: payload pequeño SIN PII (estados / etiqueta / canal / ids).

    El commit lo hace la acción de API para garantizar atomicidad.
    """
    db.add(
        ContactActivity(
            contact_id=contact_id,
            actor_user_id=actor_user_id,
            tipo=tipo,
            meta=meta,
        )
    )
