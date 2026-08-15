"""Cuando la conversación pasa a manos de una persona, el cliente se entera.

La herramienta `derivar_humano` ya avisaba al cliente con el mensaje puente.
Pero no es la única puerta a `humano`, y las otras dos lo hacían EN SILENCIO:

  - la moderación marca el mensaje → estado `humano`, cliente sin respuesta;
  - un fallo del runtime (modelo caído, respuesta vacía) → estado `humano`,
    cliente sin respuesta.

En los dos casos el cliente escribía, no recibía absolutamente nada y no tenía
forma de saber si alguien iba a contestarle.

Y un tercer silencio, distinto: si el canal NO tiene agente asignado, el
mensaje se guardaba con la conversación en estado `bot` ("ya se encarga el
robot") y el robot no existía. El hilo no salía en «Para hacer» y nadie se
enteraba de que había entrado un correo.

Lo que se comprueba aquí:
  - moderación y fallo del modelo → el cliente recibe el puente, una sola vez;
  - fallo de ENVÍO → no se intenta el puente (la tubería es lo que está roto);
  - las tres puertas marcan `derivada_a_humano_at` (métricas y orden de cola);
  - sin agente en el canal, la conversación queda para una persona.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest


def _db_available() -> bool:
    try:
        from sqlalchemy import text

        from app.db.session import db_session

        async def _check():
            async with db_session() as db:
                await db.execute(text("select 1"))

        asyncio.run(_check())
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


async def _conversacion_whatsapp():
    """Mete un WhatsApp por la puerta normal y devuelve (conv, msg_id)."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationStatus
    from app.models.message import Message
    from app.providers.whatsapp.base import IncomingMessage
    from app.services.conversation import store_incoming

    telefono = f"+3460{uuid.uuid4().int % 10_000_000:07d}"
    msg_id = await store_incoming(
        IncomingMessage(
            provider_message_id=f"wamid.{uuid.uuid4().hex}",
            from_phone=telefono,
            to_phone="+34900000000",
            message_type="text",
            text="Quiero devolver un pedido",
        )
    )
    assert msg_id is not None
    async with db_session() as db:
        conv_id = (
            await db.execute(select(Message.conversation_id).where(Message.id == msg_id))
        ).scalar_one()
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()
        conv.status = ConversationStatus.bot
        conv.spam_reviewed = True
        await db.commit()
        await db.refresh(conv)
        db.expunge(conv)
    return conv, msg_id


async def _recargar(conv_id):
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation

    async with db_session() as db:
        return (
            await db.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()


async def _puentes_guardados(conv_id) -> list[str]:
    """Mensajes del asistente marcados como puente de derivación."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.message import Message, MessageRole

    async with db_session() as db:
        msgs = (
            await db.execute(
                select(Message).where(
                    Message.conversation_id == conv_id,
                    Message.rol == MessageRole.assistant,
                )
            )
        ).scalars().all()
    return [m.contenido for m in msgs if (m.extra or {}).get("kind") == "handoff_bridge"]


# ---------------------------------------------------------------------------
# Fallos del runtime
# ---------------------------------------------------------------------------


@needs_db
@pytest.mark.asyncio
async def test_el_fallo_del_modelo_avisa_al_cliente():
    from app.models.conversation import ConversationStatus
    from app.services import conversation as conv_mod

    conv, _ = await _conversacion_whatsapp()
    enviar = AsyncMock(return_value="ext-1")

    with patch("app.services.channel_sender.send_text_to_conversation", enviar):
        await conv_mod._derivar_por_fallo(
            conv,
            event="agent.run_failed",
            message="El agente falló",
            decision="agent_run_failed",
            reason="excepción del modelo",
        )

    assert enviar.await_count == 1, "el cliente se quedó sin saber que le atiende alguien"
    despues = await _recargar(conv.id)
    assert despues.status == ConversationStatus.humano
    assert despues.derivada_a_humano_at is not None, (
        "sin esta marca la derivación no cuenta en las métricas ni ordena la cola"
    )
    assert len(await _puentes_guardados(conv.id)) == 1, (
        "el puente tiene que quedar en el panel: la operadora necesita ver qué se le dijo"
    )


@needs_db
@pytest.mark.asyncio
async def test_el_fallo_de_envio_no_intenta_el_puente():
    """Si lo que falla es el canal, el puente iría por la misma tubería rota."""
    from app.services import conversation as conv_mod

    conv, _ = await _conversacion_whatsapp()
    enviar = AsyncMock(return_value="ext-1")

    with patch("app.services.channel_sender.send_text_to_conversation", enviar):
        await conv_mod._derivar_por_fallo(
            conv,
            event="agent.send_failed",
            message="No se pudo entregar",
            decision="agent_send_failed",
            reason="el proveedor rechazó el envío",
            avisar_al_cliente=False,
        )

    enviar.assert_not_awaited()
    despues = await _recargar(conv.id)
    assert despues.derivada_a_humano_at is not None


@needs_db
@pytest.mark.asyncio
async def test_el_puente_no_se_repite_en_cada_fallo():
    """Segunda derivación con la conversación ya en manos de una persona."""
    from app.services import conversation as conv_mod

    conv, _ = await _conversacion_whatsapp()
    enviar = AsyncMock(return_value="ext-1")

    with patch("app.services.channel_sender.send_text_to_conversation", enviar):
        for _ in range(3):
            await conv_mod._derivar_por_fallo(
                conv,
                event="agent.run_failed",
                message="El agente falló",
                decision="agent_run_failed",
                reason="excepción del modelo",
            )

    assert enviar.await_count == 1, "se le mandó el mismo aviso una vez por cada fallo"


# ---------------------------------------------------------------------------
# Moderación
# ---------------------------------------------------------------------------


@needs_db
@pytest.mark.asyncio
async def test_la_moderacion_ya_no_deja_al_cliente_mudo():
    from app.models.conversation import ConversationStatus
    from app.services import message_buffer
    from app.services.conversation import process_buffered_messages
    from app.services.moderation import ModerationResult
    from tests._seed import ensure_text_agent

    await ensure_text_agent()
    conv, msg_id = await _conversacion_whatsapp()
    clave = message_buffer.conversation_key(conv.id)
    await message_buffer.push_message(clave, str(msg_id))

    marcado = AsyncMock(
        return_value=ModerationResult(flagged=True, categories=["harassment"])
    )
    enviar = AsyncMock(return_value="ext-1")
    agente = AsyncMock(return_value="no debería llamarse")

    with patch("app.services.conversation.moderate", marcado), \
        patch("app.services.conversation.run_agent", agente), \
        patch("app.services.channel_sender.send_text_to_conversation", enviar), \
        patch("app.services.conversation.is_channel_paused", AsyncMock(return_value=False)), \
        patch.object(message_buffer, "should_process", AsyncMock(return_value=True)):
        await process_buffered_messages(clave)

    agente.assert_not_awaited()
    assert enviar.await_count == 1, (
        "moderación cortó la respuesta y el cliente no recibió ninguna señal"
    )
    despues = await _recargar(conv.id)
    assert despues.status == ConversationStatus.humano
    assert despues.derivada_a_humano_at is not None


# ---------------------------------------------------------------------------
# Sin agente en el canal
# ---------------------------------------------------------------------------


@needs_db
@pytest.mark.asyncio
async def test_sin_agente_la_conversacion_queda_para_una_persona():
    from app.models.conversation import ConversationStatus
    from app.services import conversation as conv_mod

    conv, msg_id = await _conversacion_whatsapp()

    with patch.object(conv_mod, "get_runtime_for_channel", AsyncMock(return_value=None)):
        await conv_mod.handle_incoming_message(msg_id)

    despues = await _recargar(conv.id)
    assert despues.status == ConversationStatus.humano, (
        "el hilo se quedaba en «bot» y no salía en «Para hacer»: nadie se enteraba"
    )
    assert despues.derivada_a_humano_at is not None


@needs_db
@pytest.mark.asyncio
async def test_sin_agente_no_se_le_inventa_una_respuesta_al_cliente():
    """La instalación está a medias: el bot no tiene voz que poner."""
    from app.services import conversation as conv_mod

    conv, msg_id = await _conversacion_whatsapp()
    enviar = AsyncMock(return_value="ext-1")

    with patch.object(conv_mod, "get_runtime_for_channel", AsyncMock(return_value=None)), \
        patch("app.services.channel_sender.send_text_to_conversation", enviar):
        await conv_mod.handle_incoming_message(msg_id)

    enviar.assert_not_awaited()
    assert await _puentes_guardados(conv.id) == []
