"""Chequeo diferido: avisa por Web Push si una conversación entró en "Para hacer".

Lo programa `handle_incoming_message` tras cada mensaje del cliente, con un
`countdown` (buffer + margen) para que el agente ya haya actuado y veamos el
estado FINAL. Si la conversación quedó en "Para hacer" (sin contestar /
sugerencia de Entrenamiento / borrador de email / derivada a humano) → push
(con cooldown por conversación en `notify_pending`). Si el bot contestó bien,
no está pendiente → no se envía nada.

Cubre TODOS los casos con un único enganche porque reutiliza la misma
definición que la bandeja (`services/inbox_pending.conversation_needs_action`).
"""
import asyncio
import uuid

from celery import shared_task

from app.core.logging import get_logger

logger = get_logger(__name__)


@shared_task(
    name="app.tasks.notify_pending_check.check_and_notify_pending",
    bind=True,
    max_retries=1,
)
def check_and_notify_pending(self, conversation_id: str) -> None:
    try:
        asyncio.run(_check(uuid.UUID(conversation_id)))
    except Exception as exc:  # best-effort: nunca rompemos por el aviso
        logger.warning(
            "notify_pending_check.error", conversation_id=conversation_id, error=str(exc)
        )


async def _check(conversation_id: uuid.UUID) -> None:
    from sqlalchemy import desc, select

    from app.db.session import db_session
    from app.models.contact import Contact
    from app.models.conversation import Conversation
    from app.models.message import Message, MessageRole
    from app.services.inbox_pending import conversation_needs_action
    from app.services.web_push import notify_pending

    async with db_session() as db:
        if not await conversation_needs_action(db, conversation_id):
            return
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conversation_id))
        ).scalar_one_or_none()
        if conv is None:
            return
        contact = (
            await db.execute(select(Contact).where(Contact.id == conv.contact_id))
        ).scalar_one_or_none()
        last_user = (
            await db.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.rol == MessageRole.user,
                )
                .order_by(desc(Message.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()

    nombre = (contact.nombre or contact.telefono) if contact else "Cliente"
    preview = ""
    if last_user is not None:
        preview = (last_user.contenido or last_user.audio_transcript or "").strip()
    body = preview[:100] if preview else "Una conversación necesita tu atención."
    await notify_pending(
        str(conversation_id),
        f"Conversación pendiente — {nombre}",
        body,
        f"/inbox?conversation={conversation_id}",
    )
