"""Aviso de IA en la primera respuesta de los canales de texto (Reglamento UE 2024/1689, art. 50).

Vive aquí y no en `conversation.py` porque lo necesitan dos caminos de envío: la respuesta
normal del agente y el mensaje puente de `derivar_humano`. Si solo lo llevaba el primero, una
conversación que empezaba derivando (el puente era lo primero que leía el cliente) no recibía
el aviso, y como el puente ya contaba como «respuesta previa», tampoco lo recibía después.

El aviso va fuera del prompt a propósito: no depende de que el modelo recuerde presentarse.
"""
from __future__ import annotations

from sqlalchemy import select

from app.models.conversation import ConversationCanal
from app.models.message import Message, MessageRole

AVISO_IA_PRIMERA_INTERACCION = "Soy el asistente virtual del negocio, no una persona."

# Voz saluda de forma determinista en `api/voice.py`; el correo lo revisa una persona.
CANALES_CON_AVISO = {
    ConversationCanal.whatsapp,
    ConversationCanal.instagram_dm,
    ConversationCanal.web,
}


def aviso_ia_primera_interaccion(texto: str, *, es_primera: bool) -> str:
    """Añade el aviso de IA una sola vez y nunca crea un mensaje vacío."""
    if not es_primera or not texto.strip() or "asistente virtual" in texto.casefold():
        return texto
    return f"{AVISO_IA_PRIMERA_INTERACCION}\n\n{texto}"


async def aviso_ia_para_envio(db, conv, texto: str) -> str:
    """Decide con el historial persistido si este envío necesita el aviso.

    «Respuesta previa» = un mensaje del agente que el cliente SÍ recibió. No cuentan:
      - los borradores (`is_draft`): el cliente no los ha visto;
      - las sugerencias de Entrenamiento (`training`), aunque la operadora las enviara después
        y `is_draft` pasara a false: salieron sin aviso.
    """
    if conv.canal not in CANALES_CON_AVISO:
        return texto

    respuesta_previa = (
        await db.execute(
            select(Message.id)
            .where(
                Message.conversation_id == conv.id,
                Message.rol == MessageRole.assistant,
                Message.extra["is_draft"].astext.is_distinct_from("true"),
                Message.extra["training"].astext.is_distinct_from("true"),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return aviso_ia_primera_interaccion(texto, es_primera=respuesta_previa is None)
