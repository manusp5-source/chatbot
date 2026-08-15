"""Sitios donde algo fallaba y el cliente se quedaba sin respuesta, en silencio.

Todos salieron de una misma auditoría. El patrón se repite: una condición de borde, un `return` o un `continue`, y la
conversación queda con pinta de atendida mientras el cliente espera algo que no
va a llegar. Y en el panel, nada.

  - responder un correo desde el panel y que Gmail lo rechace;
  - una llamada que acaba derivada a una persona y se archiva al colgar;
  - media respuesta entregada porque la ventana de mensajería se cerró a mitad;
  - un contacto bloqueado que sigue escribiendo.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

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


needs_db = pytest.mark.skipif(not _db_available(), reason="sin base de datos")


# ---------------------------------------------------------------------------
# Media respuesta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_media_respuesta_entregada_cuenta_como_fallo():
    """La ventana de mensajería se cierra entre la parte 1 y la 2: el cliente se
    queda con la mitad de una explicación. Como algo llegó, esto no contaba
    como fallo y la conversación seguía en manos del bot."""
    from app.services import conversation as conv_mod
    from app.services.channel_sender import MessagingWindowClosed

    conv = MagicMock()
    conv.id = uuid.uuid4()
    conv.canal = MagicMock(value="whatsapp")
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()

    envios = {"n": 0}

    async def _send(_conv, _texto):
        envios["n"] += 1
        if envios["n"] == 1:
            return "wamid.1"
        raise MessagingWindowClosed("han pasado más de 24 h")

    with patch.object(conv_mod, "push_runtime_log", AsyncMock()):
        entregadas = await conv_mod.deliver_response_parts(
            db, conv, ["primera parte", "segunda parte", "tercera"], send=_send, pause=0
        )

    assert entregadas == 1, "se entregó más de lo que debería"
    # Y lo importante: quien llama tiene que poder distinguir esto de un envío
    # completo. `deliver_response_parts` devuelve cuántas salieron, y el
    # llamante compara con cuántas tenían texto.
    assert entregadas < 3


# ---------------------------------------------------------------------------
# La llamada derivada
# ---------------------------------------------------------------------------


@needs_db
@pytest.mark.asyncio
async def test_una_llamada_derivada_no_se_archiva_al_colgar():
    """Archivarla la sacaba de «Para hacer» y de todos los contadores: al
    cliente se le decía que le atenderían y no le atendía nadie."""
    from sqlalchemy import select

    from app.api.voice import _close_call_conversation
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import (
        Conversation,
        ConversationCanal,
        ConversationStatus,
    )

    call_id = f"call_{uuid.uuid4().hex}"
    async with db_session() as db:
        contacto = Contact(
            telefono=f"+3465{uuid.uuid4().int % 10_000_000:07d}",
            origen=ContactOrigen.whatsapp,
        )
        db.add(contacto)
        await db.flush()
        conv = Conversation(
            contact_id=contacto.id,
            canal=ConversationCanal.retell_voice,
            session_id=call_id,
            status=ConversationStatus.humano,
        )
        db.add(conv)
        await db.commit()
        conv_id = conv.id

    await _close_call_conversation(call_id)

    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()
        assert conv.ended_at is not None, "la llamada tiene que quedar cerrada"
        assert conv.archived_at is None, (
            "la llamada derivada se archivó: nadie va a devolverla"
        )


@needs_db
@pytest.mark.asyncio
async def test_una_llamada_normal_si_se_archiva_al_colgar():
    """Sin esto, cada llamada dejaba una conversación viva para siempre."""
    from sqlalchemy import select

    from app.api.voice import _close_call_conversation
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import (
        Conversation,
        ConversationCanal,
        ConversationStatus,
    )

    call_id = f"call_{uuid.uuid4().hex}"
    async with db_session() as db:
        contacto = Contact(
            telefono=f"+3466{uuid.uuid4().int % 10_000_000:07d}",
            origen=ContactOrigen.whatsapp,
        )
        db.add(contacto)
        await db.flush()
        db.add(
            Conversation(
                contact_id=contacto.id,
                canal=ConversationCanal.retell_voice,
                session_id=call_id,
                status=ConversationStatus.bot,
            )
        )
        await db.commit()

    await _close_call_conversation(call_id)

    async with db_session() as db:
        conv = (
            await db.execute(
                select(Conversation).where(Conversation.session_id == call_id)
            )
        ).scalar_one()
        assert conv.archived_at is not None


# ---------------------------------------------------------------------------
# El contacto bloqueado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_un_bloqueado_que_sigue_escribiendo_deja_rastro():
    """El descarte era mudo: solo se avisaba al bloquear. A partir de ahí ese
    contacto escribía y no existía en ninguna parte, así que un bloqueo puesto
    por error no había forma de detectarlo."""
    from app.services import conversation as conv_mod
    from app.services.agent_guardrails import VERDICT_BLOCKED

    avisos: list[dict] = []

    async def _avisar(**kwargs):
        avisos.append(kwargs)

    incoming = MagicMock()
    incoming.from_phone = "+34600111222"

    with patch.object(
        conv_mod, "evaluate_incoming", AsyncMock(return_value=VERDICT_BLOCKED)
    ), patch.object(conv_mod, "notify_security", _avisar):
        assert await conv_mod.store_incoming(incoming) is None

    assert avisos, "el mensaje de un bloqueado se tiraba sin una sola línea"
    assert avisos[0]["kind"] == "blocked_contact_message"
    # Con freno por contacto: bloquear a alguien es, casi siempre, porque manda
    # mucho. Sin el freno el aviso sería el mismo flood que se está bloqueando.
    assert avisos[0]["throttle_key"] == "+34600111222"


def test_el_canal_se_deduce_del_identificador_sin_soltar_el_telefono():
    from app.services.conversation import _canal_del_identificador

    assert _canal_del_identificador("email:ana@x.com") == "correo"
    assert _canal_del_identificador("ig:12345") == "Instagram"
    assert _canal_del_identificador("web:abc") == "chat web"
    assert _canal_del_identificador("+34600111222") == "WhatsApp"
    assert _canal_del_identificador("wa:ES.123") == "WhatsApp"
