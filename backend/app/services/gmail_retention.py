"""Retención/purga del canal Email (Gmail) — minimización de datos.

Modelo (decisión del dueño, FIJA): Gmail es el ARCHIVO; la BD es una CACHÉ. A
los `EMAIL_RETENTION_MONTHS` meses (6 por defecto) se purga el CONTENIDO del
correo de la BD pero se conserva una entrada ligera para el histórico del CRM y
para poder recuperarlo bajo demanda desde Gmail:

  - Purgar:   Message.contenido → "" ; extra.html_body → eliminado.
  - Conservar: extra.provider_message_id (id de Gmail), extra.subject,
    extra.rfc822_message_id, in_reply_to, references, created_at, rol y el
    vínculo a la conversación/contacto. Marca extra.purged = True y
    extra.purged_at = <iso>.

Recuperar el contenido (bajo demanda) se hace desde el endpoint
`/conversations/{id}/messages/{mid}/recover`, que vuelve a traerlo de Gmail por
su id y lo MUESTRA sin re-guardarlo (la minimización se mantiene).

Solo afecta al canal **email**. NO toca WhatsApp/IG/web/voz. El borrado total
por contacto (cascade) sigue existiendo y es aparte (supresión, no minimización).

Esta purga es IDEMPOTENTE: re-ejecutarla no rompe nada (los ya purgados se
saltan por el filtro extra.purged). Corre una vez al día desde Celery beat
(ver app/tasks/purge_emails.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import db_session
from app.models.conversation import Conversation, ConversationCanal
from app.models.message import Message

logger = get_logger(__name__)

# Tamaño de lote: purgamos y hacemos commit por lotes para no bloquear la BD ni
# mantener una transacción enorme abierta si hay mucho histórico acumulado.
_BATCH_SIZE = 200


def _has_content():
    """Predicado SQL: Message.contenido no es NULL ni cadena vacía.

    Aislado para mantener el WHERE legible. Un correo ya purgado tiene
    contenido == '', así que este predicado también colabora con la
    idempotencia (no vuelve a seleccionar lo ya vaciado)."""
    return (Message.contenido.is_not(None)) & (Message.contenido != "")


def _not_pending_draft():
    """Predicado SQL: el mensaje NO es un borrador pendiente de enviar.

    Pendiente = extra.is_draft == true y extra.draft_sent != true. Cubre tanto
    los borradores reales de Gmail como las sugerencias del modo Entrenamiento
    (que usan el mismo mecanismo).
    """
    return or_(
        Message.extra["is_draft"].astext.is_(None),
        Message.extra["is_draft"].astext != "true",
        Message.extra["draft_sent"].astext == "true",
    )


async def purge_old_emails() -> dict:
    """Purga el contenido de los correos (canal email) más viejos que el umbral
    de retención. Devuelve {"purged": n}.

    Selección: Message que (1) pertenecen a una Conversation con canal == email
    (JOIN), (2) created_at < cutoff, (3) NO purgados aún (extra->>'purged' es
    NULL o != 'true'), y (4) tienen contenido (contenido != '' o existe
    extra->>'html_body'). De cada uno se vacía el cuerpo y se eliminan los
    campos de contenido del extra, conservando los metadatos ligeros.
    """
    # 0 = NO PURGAR. Es lo que entiende cualquiera al escribir 0, y era justo lo
    # contrario de lo que pasaba: `max(0, 0)` daba corte = "ahora" y la primera
    # pasada vaciaba el contenido de TODOS los correos, incluidos los de hoy.
    # No había validación en ninguna parte.
    configured = int(settings.EMAIL_RETENTION_MONTHS)
    if configured <= 0:
        logger.info("email.retention.disabled")
        return {"purged": 0, "status": "disabled"}

    # Suelo de seguridad: por debajo del mínimo la purga se sube a él. Una
    # retención de días no da margen ni para revisar un hilo de la semana
    # pasada, y el cuerpo solo se recupera si el correo sigue vivo en Gmail.
    min_months = max(1, int(settings.EMAIL_RETENTION_MIN_MONTHS))
    months = max(min_months, configured)
    if months != configured:
        logger.warning(
            "email.retention.clamped", configured=configured, applied=months
        )

    # Aproximamos "N meses" con timedelta(days=30*N). El proyecto NO declara
    # python-dateutil como dependencia, así que NO usamos relativedelta para no
    # introducir un import oculto no garantizado en producción. La pequeña
    # imprecisión (un mes ~30 días) es irrelevante para una política de 6 meses.
    cutoff = datetime.now(timezone.utc) - timedelta(days=30 * months)

    purged_count = 0

    async with db_session() as db:
        while True:
            # Seleccionamos un lote de mensajes de email candidatos a purgar.
            # JOIN con Conversation para filtrar SOLO el canal email.
            stmt = (
                select(Message)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(
                    Conversation.canal == ConversationCanal.email,
                    Message.created_at < cutoff,
                    # No purgados aún: extra->>'purged' IS NULL o distinto de 'true'.
                    or_(
                        Message.extra["purged"].astext.is_(None),
                        Message.extra["purged"].astext != "true",
                    ),
                    # Con contenido que purgar: texto no vacío O html_body presente.
                    or_(
                        _has_content(),
                        Message.extra["html_body"].astext.is_not(None),
                    ),
                    # NUNCA los borradores pendientes de enviar. Un borrador sin
                    # revisar es trabajo por hacer, no archivo: al vaciarlo la
                    # tarjeta salía en blanco y no había de dónde recuperarlo
                    # (Gmail lo tiene, pero el operador ya no sabe qué decía).
                    # Un borrador YA enviado sí es histórico y se purga normal.
                    _not_pending_draft(),
                )
                .limit(_BATCH_SIZE)
            )
            rows = (await db.execute(stmt)).scalars().all()
            if not rows:
                break

            for msg in rows:
                # Reconstruimos el extra como dict NUEVO y lo reasignamos para que
                # SQLAlchemy detecte el cambio del JSONB (no usamos MutableDict).
                extra = dict(msg.extra or {})
                # Eliminamos el cuerpo HTML; conservamos el resto de metadatos
                # (provider_message_id, subject, rfc822_message_id, in_reply_to,
                # references, attachments, etc.).
                extra.pop("html_body", None)
                extra["purged"] = True
                extra["purged_at"] = datetime.now(timezone.utc).isoformat()

                msg.contenido = ""
                msg.extra = extra
                purged_count += 1

            # Commit por lote: libera la transacción y evita bloqueos largos.
            await db.commit()

            # Si el lote no llenó el tamaño máximo, ya no quedan más.
            if len(rows) < _BATCH_SIZE:
                break

    # Logueamos SOLO el conteo (nunca contenido) — minimización de PII en logs.
    logger.info("email.retention.purged", count=purged_count)
    return {"purged": purged_count}
