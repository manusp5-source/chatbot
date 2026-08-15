"""Resumen rodante de conversaciones largas.

La ventana de contexto del agente son los últimos N mensajes. En conversaciones
largas eso pierde el inicio (nombre del cliente, motivo, acuerdos previos). Este
módulo mantiene un resumen incremental de los mensajes que YA cayeron fuera de
la ventana y lo inyecta en el prompt del agente.

Diseño:
  - `Conversation.rolling_summary` = texto condensado (cifrado en reposo).
  - `Conversation.rolling_summary_upto` = nº de mensajes más antiguos ya
    cubiertos por el resumen. Para actualizar en incrementos y NO re-resumir
    todo cada vez (coste acotado por llamada).
  - Solo se actualiza cuando se acumulan ≥ SUMMARY_BATCH mensajes nuevos fuera
    de la ventana → una llamada LLM ocasional y barata (modelo pequeño).
Todo es best-effort: si el resumen falla, el agente responde igual (sin él).
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.core.logging import get_logger
from app.db.session import db_session
from app.models.conversation import Conversation
from app.models.message import Message, MessageRole
from app.providers.llm import resolve_llm_provider
from app.providers.llm.base import LLMMessage

logger = get_logger(__name__)

# Mínimo de mensajes nuevos fuera de la ventana antes de re-resumir (acota el
# coste: una llamada cada ~SUMMARY_BATCH mensajes que caen del contexto).
SUMMARY_BATCH = 10
# Tope del resumen para no inflar el prompt.
_MAX_SUMMARY_CHARS = 900
# Tope de mensajes por llamada de resumen (defensa de coste).
_MAX_MSGS_PER_CALL = 40


def _content(m: Message) -> str | None:
    if (m.extra or {}).get("is_draft") and not (m.extra or {}).get("draft_sent"):
        return None
    return m.contenido or m.audio_transcript


async def maybe_update_rolling_summary(
    conversation_id: uuid.UUID,
    *,
    context_window: int,
    model: str | None,
    llm_provider_id: uuid.UUID | None,
) -> str | None:
    """Actualiza el resumen rodante si hace falta y devuelve el resumen vigente
    (o None si no hay). No lanza: ante cualquier fallo devuelve el resumen que
    hubiera y no bloquea la respuesta del agente."""
    try:
        async with db_session() as db:
            conv = (
                await db.execute(select(Conversation).where(Conversation.id == conversation_id))
            ).scalar_one_or_none()
            if not conv:
                return None
            total = (
                await db.execute(
                    select(func.count(Message.id)).where(Message.conversation_id == conversation_id)
                )
            ).scalar_one()

            # Mensajes que quedan FUERA de la ventana de contexto (los más
            # antiguos). Solo esos se resumen.
            older_count = max(0, int(total) - int(context_window))
            already = int(conv.rolling_summary_upto or 0)
            new_older = older_count - already
            if new_older < SUMMARY_BATCH:
                # No hay suficientes mensajes nuevos fuera de la ventana.
                return conv.rolling_summary or None

            # Trae SOLO los mensajes nuevos a resumir: posiciones [already, older_count).
            batch = min(new_older, _MAX_MSGS_PER_CALL)
            rows = (
                await db.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.created_at.asc())
                    .offset(already)
                    .limit(batch)
                )
            ).scalars().all()
            prev_summary = conv.rolling_summary or ""

        lines: list[str] = []
        for m in rows:
            c = _content(m)
            if not c:
                continue
            who = "Cliente" if m.rol == MessageRole.user else "Agente/Operador"
            lines.append(f"{who}: {c[:500]}")
        if not lines:
            # Todo el lote eran borradores/vacíos: avanza el puntero igual para
            # no reintentar el mismo tramo indefinidamente.
            async with db_session() as db:
                conv = (
                    await db.execute(select(Conversation).where(Conversation.id == conversation_id))
                ).scalar_one_or_none()
                if conv:
                    conv.rolling_summary_upto = already + batch
                    await db.commit()
            return prev_summary or None

        prompt = (
            "Eres un asistente que mantiene un RESUMEN BREVE del principio de una "
            "conversación de atención al cliente, para que otro agente no pierda el "
            "contexto. Integra el resumen previo (si lo hay) con los mensajes nuevos "
            "en un único resumen conciso en español (máx. 6 frases). Conserva SOLO lo "
            "útil para continuar: nombre y datos del cliente, qué pide, decisiones o "
            "compromisos, y estado. No inventes. No incluyas saludos ni relleno."
        )
        user = f"RESUMEN PREVIO:\n{prev_summary or '(ninguno)'}\n\nMENSAJES NUEVOS:\n" + "\n".join(lines)

        llm = await resolve_llm_provider(llm_provider_id)
        resp = await llm.complete(
            messages=[
                LLMMessage(role="system", content=prompt),
                LLMMessage(role="user", content=user),
            ],
            model=model,
            temperature=0.2,
            max_tokens=300,
            source="rolling_summary",
        )
        new_summary = (resp.content or "").strip()[:_MAX_SUMMARY_CHARS]
        if not new_summary:
            return prev_summary or None

        async with db_session() as db:
            conv = (
                await db.execute(select(Conversation).where(Conversation.id == conversation_id))
            ).scalar_one_or_none()
            if conv:
                conv.rolling_summary = new_summary
                conv.rolling_summary_upto = already + batch
                await db.commit()
        logger.info(
            "rolling_summary.updated",
            conversation_id=str(conversation_id),
            upto=already + batch,
            chars=len(new_summary),
        )
        return new_summary
    except Exception as e:
        logger.warning("rolling_summary.error", conversation_id=str(conversation_id), error=str(e))
        return None
