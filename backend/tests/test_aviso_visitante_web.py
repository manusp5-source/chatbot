"""El visitante del chat web no puede quedarse mudo sin ninguna señal.

En WhatsApp o Instagram el cliente tiene su hilo abierto y sabe que alguien
acabará leyéndole. En la web es una persona anónima mirando una burbuja: si el
runtime corta (sin agente configurado, conversación en cuarentena, hilo que ya
atiende una persona, canal pausado) no recibía absolutamente nada y se iba. Y
si la dueña pausa el bot desde el panel, el widget de TODAS las webs de sus
clientes se queda en silencio a la vez.

Reglas que se comprueban aquí:
  - el aviso se publica SOLO en el canal web (WhatsApp no se entera de nada);
  - cuando la conversación ya la atiende una persona, el texto lo dice;
  - el aviso NO se persiste como mensaje del asistente: si se guardara,
    entraría en el historial que lee el agente.
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


class _BusFalso:
    """Recoge lo que se publica al bus, para poder mirar el canal del widget."""

    def __init__(self) -> None:
        self.publicado: list[tuple] = []

    async def publish(self, channel, event, payload):
        self.publicado.append((channel, event, payload))

    def avisos_web(self, conversation_id) -> list[str]:
        chan = f"webchat:{conversation_id}"
        return [
            p.get("text")
            for c, ev, p in self.publicado
            if c == chan and ev == "message.out"
        ]


def _incoming(phone: str, **kw):
    from app.providers.whatsapp.base import IncomingMessage

    base = dict(
        provider_message_id=f"wamid.{uuid.uuid4().hex}",
        from_phone=phone,
        to_phone="+34900000000",
        message_type="text",
        text="¿Hacéis envíos a Canarias?",
    )
    base.update(kw)
    return IncomingMessage(**base)


async def _conversacion_web(status=None):
    """Crea una conversación WEB real con un mensaje del visitante."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationStatus
    from app.services import conversation as conv_mod

    visitor = f"web:{uuid.uuid4()}"
    msg_id = await conv_mod.store_incoming(_incoming(visitor))
    assert msg_id is not None
    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.session_id == visitor))
        ).scalar_one()
        conv.status = status or ConversationStatus.bot
        conv.spam_reviewed = True
        await db.commit()
        return conv.id, msg_id


async def _mensajes_de_asistente(conv_id) -> int:
    from sqlalchemy import func, select

    from app.db.session import db_session
    from app.models.message import Message, MessageRole

    async with db_session() as db:
        return (
            await db.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == conv_id, Message.rol == MessageRole.assistant)
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# Entrada del mensaje
# ---------------------------------------------------------------------------


@needs_db
@pytest.mark.asyncio
async def test_sin_agente_configurado_el_visitante_recibe_aviso():
    from app.services import conversation as conv_mod

    conv_id, msg_id = await _conversacion_web()
    bus = _BusFalso()

    with patch.object(conv_mod, "event_bus", bus), \
        patch.object(conv_mod, "get_runtime_for_channel", AsyncMock(return_value=None)):
        await conv_mod.handle_incoming_message(msg_id)

    assert bus.avisos_web(conv_id) == [conv_mod.WEB_NOTICE_GENERIC]
    assert await _mensajes_de_asistente(conv_id) == 0, (
        "el aviso se guardó como respuesta del asistente y ensucia el contexto"
    )


@needs_db
@pytest.mark.asyncio
async def test_conversacion_en_cuarentena_avisa_al_visitante():
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation
    from app.services import conversation as conv_mod
    from tests._seed import ensure_text_agent

    await ensure_text_agent()
    conv_id, msg_id = await _conversacion_web()
    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()
        conv.quarantined_at = datetime.now(timezone.utc)
        await db.commit()

    bus = _BusFalso()
    with patch.object(conv_mod, "event_bus", bus):
        await conv_mod.handle_incoming_message(msg_id)

    assert bus.avisos_web(conv_id) == [conv_mod.WEB_NOTICE_GENERIC]


@needs_db
@pytest.mark.asyncio
async def test_si_ya_la_atiende_una_persona_se_dice_tal_cual():
    """El genérico ("lo revisará alguien") diría lo contrario de lo que pasa."""
    from app.models.conversation import ConversationStatus
    from app.services import conversation as conv_mod
    from tests._seed import ensure_text_agent

    await ensure_text_agent()
    conv_id, msg_id = await _conversacion_web(status=ConversationStatus.humano)

    bus = _BusFalso()
    with patch.object(conv_mod, "event_bus", bus):
        await conv_mod.handle_incoming_message(msg_id)

    assert bus.avisos_web(conv_id) == [conv_mod.WEB_NOTICE_HUMAN]
    assert "persona" in conv_mod.WEB_NOTICE_HUMAN


@needs_db
@pytest.mark.asyncio
async def test_canal_pausado_avisa_al_visitante():
    from app.services import conversation as conv_mod
    from tests._seed import ensure_text_agent

    await ensure_text_agent()
    conv_id, msg_id = await _conversacion_web()

    bus = _BusFalso()
    with patch.object(conv_mod, "event_bus", bus), \
        patch.object(conv_mod, "is_channel_paused", AsyncMock(return_value=True)), \
        patch.object(conv_mod, "is_budget_pause_active", AsyncMock(return_value=False)), \
        patch.object(conv_mod, "is_in_demo_whitelist", AsyncMock(return_value=False)):
        await conv_mod.handle_incoming_message(msg_id)

    assert bus.avisos_web(conv_id) == [conv_mod.WEB_NOTICE_GENERIC]


@needs_db
@pytest.mark.asyncio
async def test_en_whatsapp_no_se_publica_ningun_aviso():
    """El aviso es solo para el widget: el cliente de WhatsApp tiene su hilo."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationStatus
    from app.services import conversation as conv_mod
    from tests._seed import ensure_text_agent

    await ensure_text_agent()
    phone = f"+3464{uuid.uuid4().int % 10_000_000:07d}"
    msg_id = await conv_mod.store_incoming(_incoming(phone))
    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.session_id == phone))
        ).scalar_one()
        conv.status = ConversationStatus.humano
        await db.commit()
        conv_id = conv.id

    bus = _BusFalso()
    with patch.object(conv_mod, "event_bus", bus):
        await conv_mod.handle_incoming_message(msg_id)

    assert bus.avisos_web(conv_id) == []


# ---------------------------------------------------------------------------
# Drenado del buffer (los mismos cortes, unos segundos más tarde)
# ---------------------------------------------------------------------------


@needs_db
@pytest.mark.asyncio
async def test_drenado_sin_agente_configurado_avisa():
    from app.services import conversation as conv_mod
    from app.services import message_buffer

    conv_id, msg_id = await _conversacion_web()
    buffer_key = message_buffer.conversation_key(conv_id)
    await message_buffer.push_message(buffer_key, str(msg_id))

    bus = _BusFalso()
    with patch.object(conv_mod, "event_bus", bus), \
        patch.object(conv_mod, "get_runtime_for_channel", AsyncMock(return_value=None)):
        await conv_mod.process_buffered_messages(buffer_key)

    assert bus.avisos_web(conv_id) == [conv_mod.WEB_NOTICE_GENERIC]


@needs_db
@pytest.mark.asyncio
async def test_drenado_con_la_conversacion_ya_en_manos_de_una_persona():
    from app.models.conversation import ConversationStatus
    from app.services import conversation as conv_mod
    from app.services import message_buffer
    from tests._seed import ensure_text_agent

    await ensure_text_agent()
    conv_id, msg_id = await _conversacion_web(status=ConversationStatus.humano)
    buffer_key = message_buffer.conversation_key(conv_id)
    await message_buffer.push_message(buffer_key, str(msg_id))

    bus = _BusFalso()
    run = AsyncMock(return_value="no debería llamarse")
    with patch.object(conv_mod, "event_bus", bus), \
        patch.object(conv_mod, "run_agent", run), \
        patch.object(message_buffer, "should_process", AsyncMock(return_value=True)):
        await conv_mod.process_buffered_messages(buffer_key)

    run.assert_not_called()
    assert bus.avisos_web(conv_id) == [conv_mod.WEB_NOTICE_HUMAN]
    assert await _mensajes_de_asistente(conv_id) == 0


@needs_db
@pytest.mark.asyncio
async def test_drenado_con_el_canal_pausado_avisa():
    from app.services import conversation as conv_mod
    from app.services import message_buffer
    from tests._seed import ensure_text_agent

    await ensure_text_agent()
    conv_id, msg_id = await _conversacion_web()
    buffer_key = message_buffer.conversation_key(conv_id)
    await message_buffer.push_message(buffer_key, str(msg_id))

    bus = _BusFalso()
    run = AsyncMock(return_value="no debería llamarse")
    with patch.object(conv_mod, "event_bus", bus), \
        patch.object(conv_mod, "run_agent", run), \
        patch.object(conv_mod, "is_channel_paused", AsyncMock(return_value=True)), \
        patch.object(conv_mod, "is_budget_pause_active", AsyncMock(return_value=False)), \
        patch.object(conv_mod, "is_in_demo_whitelist", AsyncMock(return_value=False)), \
        patch.object(conv_mod, "is_channel_training", AsyncMock(return_value=False)), \
        patch.object(message_buffer, "should_process", AsyncMock(return_value=True)):
        await conv_mod.process_buffered_messages(buffer_key)

    run.assert_not_called()
    assert bus.avisos_web(conv_id) == [conv_mod.WEB_NOTICE_GENERIC]
