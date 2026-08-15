"""Ningún mensaje del cliente se pierde por el camino de ENTRADA.

Cubre los agujeros de la ingesta:

  5. Una ráfaga normal (15 fotos de un producto) descartaba los mensajes ANTES
     de guardarlos —no existían en ninguna parte— y a las 5 ráfagas bloqueaba
     al contacto 24 horas.
  8. El marcador antiduplicados se ponía antes de terminar el trabajo: si el
     broker no aceptaba la tarea, el mensaje quedaba en la BD sin procesar para
     siempre porque el reintento del proveedor se descartaba por duplicado.
  9. Dos mensajes simultáneos de un contacto NUEVO: consulta-luego-inserción
     sin protección → error de integridad, 500 y mensaje perdido; o dos
     conversaciones abiertas con la pregunta en un hilo y la respuesta en otro.
 10. Cerrar la conversación durante el buffer descartaba los mensajes.
 11. Una foto sin pie de foto era silencio absoluto: ni el cliente recibía
     nada ni el agente se enteraba de que había entrado una imagen.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
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


def _incoming(phone: str, **kw):
    from app.providers.whatsapp.base import IncomingMessage

    base = dict(
        provider_message_id=f"wamid.{uuid.uuid4().hex}",
        from_phone=phone,
        to_phone="+34900000000",
        message_type="text",
        text="Hola",
    )
    base.update(kw)
    return IncomingMessage(**base)


def _phone() -> str:
    return f"+3464{uuid.uuid4().int % 10_000_000:07d}"


# ---------------------------------------------------------------------------
# 5) Ráfagas: se guardan, no bloquean 24 h
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_una_rafaga_normal_no_bloquea_al_contacto():
    """15 fotos seguidas es un caso normal. Los mensajes de más no se
    contestan, pero el contacto NO entra en la lista de bloqueados."""
    from app.services import agent_guardrails as g

    phone = f"rafaga-{uuid.uuid4().hex[:8]}"
    veredictos = [
        await g.evaluate_incoming(phone)
        for _ in range(g.MAX_MESSAGES_PER_MINUTE + 5)
    ]

    assert veredictos[: g.MAX_MESSAGES_PER_MINUTE] == [g.VERDICT_ACCEPT] * g.MAX_MESSAGES_PER_MINUTE
    assert veredictos[-1] == g.VERDICT_THROTTLE
    assert not await g.is_phone_blocked(phone), (
        "una ráfaga legítima ha bloqueado al contacto"
    )


@pytest.mark.asyncio
async def test_el_flood_de_verdad_si_bloquea_pero_solo_una_hora():
    """Muy por encima del tope ya no es una persona escribiendo. Se bloquea,
    pero una hora: antes eran 24 h y había que desbloquear a mano."""
    from app.core.redis import get_redis
    from app.services import agent_guardrails as g

    phone = f"flood-{uuid.uuid4().hex[:8]}"
    total = g.ABUSE_MESSAGES_PER_MINUTE + g.BLOCK_TRIGGER_HITS + 1
    with patch.object(g, "notify_security", AsyncMock()):
        veredictos = [await g.evaluate_incoming(phone) for _ in range(total)]

    assert veredictos[-1] == g.VERDICT_BLOCKED
    assert await g.is_phone_blocked(phone)
    ttl = await get_redis().ttl(f"guard:blocked:{phone}")
    assert 0 < ttl <= g.AUTO_BLOCK_TTL_SECS <= 3600
    await g.unblock_phone(phone)


@needs_db
@pytest.mark.asyncio
async def test_el_mensaje_de_una_rafaga_se_guarda_aunque_no_se_conteste():
    """Antes devolvía None ANTES de guardar: el mensaje no existía ni en la
    bandeja, ni en 'Para hacer', ni en Monitorización."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation
    from app.models.message import Message
    from app.services import conversation as conv_mod
    from app.services.agent_guardrails import VERDICT_THROTTLE

    phone = _phone()
    incoming = _incoming(phone, text="foto 11 de 15")

    with patch.object(conv_mod, "evaluate_incoming", AsyncMock(return_value=VERDICT_THROTTLE)):
        msg_id = await conv_mod.store_incoming(incoming)

    # None: el llamante NO encola el agente (no se contesta).
    assert msg_id is None
    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.session_id == phone))
        ).scalar_one()
        msg = (
            await db.execute(select(Message).where(Message.conversation_id == conv.id))
        ).scalar_one()
        assert msg.contenido == "foto 11 de 15", "el mensaje del cliente se perdió"
        assert msg.extra.get("rate_limited") is True


# ---------------------------------------------------------------------------
# 8) El marcador antiduplicados no puede tirar un mensaje sin procesar
# ---------------------------------------------------------------------------


@needs_db
@pytest.mark.asyncio
async def test_un_mensaje_guardado_pero_no_encolado_se_recupera():
    """El broker no aceptó la tarea: el reintento del proveedor tiene que poder
    reencolarlo en vez de verse descartado por duplicado."""
    from app.core.redis import get_redis
    from app.services.conversation import mark_agent_queued, store_incoming

    phone = _phone()
    incoming = _incoming(phone)
    first = await store_incoming(incoming)
    assert first is not None

    # Simulamos el reintento del proveedor: el cerrojo en Redis ya expiró.
    await get_redis().delete(f"dedupe:msg:{incoming.provider_message_id}")

    # Como nadie confirmó el encolado (agent_queued=False), devuelve el MISMO
    # id para que el llamante lo encole ahora.
    again = await store_incoming(incoming)
    assert again == first, "el mensaje se habría quedado sin procesar para siempre"

    # Una vez encolado de verdad, el duplicado vuelve a descartarse.
    await mark_agent_queued(first)
    await get_redis().delete(f"dedupe:msg:{incoming.provider_message_id}")
    assert await store_incoming(incoming) is None


@needs_db
@pytest.mark.asyncio
async def test_el_rescate_funciona_sin_tocar_redis_a_mano():
    """El caso de arriba borraba el cerrojo a mano para que el rescate llegara
    a dispararse. Sin ese borrado NO se disparaba nunca: al guardar bien se
    alargaba el cerrojo a 24 h aunque el encolado no hubiera ocurrido, y el
    reintento del proveedor salía por duplicado antes de llegar a la rama que
    rescata. O sea, código muerto justo en el escenario para el que se escribió.

    Ahora el cerrojo solo se alarga cuando el broker ha aceptado la tarea."""
    from app.core.redis import get_redis
    from app.services.conversation import mark_agent_queued, store_incoming

    phone = _phone()
    incoming = _incoming(phone)
    first = await store_incoming(incoming)
    assert first is not None

    ttl = await get_redis().ttl(f"dedupe:msg:{incoming.provider_message_id}")
    assert 0 < ttl <= 300, (
        "el cerrojo se alargó a 24 h sin que nadie hubiera encolado nada"
    )

    await mark_agent_queued(first)
    ttl = await get_redis().ttl(f"dedupe:msg:{incoming.provider_message_id}")
    assert ttl > 300, "encolado de verdad y el cerrojo sigue caducando en cinco minutos"


@needs_db
@pytest.mark.asyncio
async def test_el_cerrojo_se_suelta_si_la_ingesta_revienta():
    """Si el guardado falla a mitad, el reintento del proveedor NO puede
    encontrarse el mensaje marcado como visto."""
    from app.core.redis import get_redis
    from app.services import conversation as conv_mod

    incoming = _incoming(_phone())
    with patch.object(
        conv_mod, "_store_incoming_locked", AsyncMock(side_effect=RuntimeError("BD caída"))
    ):
        with pytest.raises(RuntimeError):
            await conv_mod.store_incoming(incoming)

    assert not await get_redis().exists(f"dedupe:msg:{incoming.provider_message_id}")


# ---------------------------------------------------------------------------
# 9) Dos mensajes simultáneos de un contacto NUEVO
# ---------------------------------------------------------------------------


@needs_db
@pytest.mark.asyncio
async def test_dos_mensajes_simultaneos_de_un_contacto_nuevo():
    """Los dos se guardan, con UN solo contacto y UNA sola conversación."""
    from sqlalchemy import func, select

    from app.db.session import db_session
    from app.models.contact import Contact
    from app.models.conversation import Conversation
    from app.models.message import Message
    from app.services.conversation import store_incoming

    phone = _phone()
    ids = await asyncio.gather(
        store_incoming(_incoming(phone, text="hola")),
        store_incoming(_incoming(phone, text="¿estáis?")),
        return_exceptions=True,
    )

    assert all(not isinstance(i, Exception) for i in ids), f"error de integridad: {ids}"
    assert all(i is not None for i in ids), "se perdió uno de los dos mensajes"

    async with db_session() as db:
        contactos = (
            await db.execute(
                select(func.count()).select_from(Contact).where(Contact.telefono == phone)
            )
        ).scalar_one()
        assert contactos == 1
        convs = (
            await db.execute(select(Conversation).where(Conversation.session_id == phone))
        ).scalars().all()
        assert len(convs) == 1, "se abrieron dos conversaciones para el mismo contacto"
        mensajes = (
            await db.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == convs[0].id)
            )
        ).scalar_one()
        assert mensajes == 2


# ---------------------------------------------------------------------------
# 10) Cerrar la conversación durante el buffer
# ---------------------------------------------------------------------------


@needs_db
@pytest.mark.asyncio
async def test_cerrar_la_conversacion_durante_el_buffer_deja_traza():
    """Los mensajes ya drenados no se contestan (la conversación está cerrada),
    pero se resuelven desde el propio mensaje y queda log visible en vez de un
    `return` mudo por "no encuentro conversación abierta"."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationStatus
    from app.services import conversation as conv_mod
    from app.services import message_buffer
    from tests._seed import ensure_text_agent

    await ensure_text_agent()

    phone = _phone()
    msg_id = await conv_mod.store_incoming(_incoming(phone, text="una cosita más"))
    assert msg_id is not None

    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.session_id == phone))
        ).scalar_one()
        conv.status = ConversationStatus.cerrada  # la operadora la cierra
        conv.spam_reviewed = True
        await db.commit()

    await message_buffer.push_message(phone, str(msg_id))
    logs: list[dict] = []

    async def _fake_log(**kw):
        logs.append(kw)

    with patch.object(conv_mod, "push_runtime_log", _fake_log), \
        patch.object(conv_mod, "run_agent", AsyncMock(return_value="no debería llamarse")) as run, \
        patch.object(message_buffer, "should_process", AsyncMock(return_value=True)):
        await conv_mod.process_buffered_messages(phone)

    run.assert_not_called()
    assert any(log.get("event") == "drain.conversation_closed" for log in logs), (
        "la conversación se cerró durante el buffer y los mensajes desaparecieron "
        "sin dejar rastro"
    )


# ---------------------------------------------------------------------------
# 11) Adjunto sin pie de foto
# ---------------------------------------------------------------------------


def test_un_adjunto_sin_texto_se_le_cuenta_al_agente():
    from app.services.conversation import _media_placeholder

    assert "imagen" in _media_placeholder("image")
    assert "documento" in _media_placeholder("document")


@needs_db
@pytest.mark.asyncio
async def test_una_foto_sin_pie_de_foto_no_es_silencio():
    """Antes: texto combinado vacío → `return` sin decir nada al cliente ni al
    agente, que ni se enteraba de que había entrado una imagen."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationStatus
    from app.services import conversation as conv_mod
    from app.services import message_buffer
    from tests._seed import ensure_text_agent

    await ensure_text_agent()

    phone = _phone()
    msg_id = await conv_mod.store_incoming(
        _incoming(
            phone,
            message_type="image",
            text=None,
            media_kind="image",
            media_url="https://api.ycloud.com/media/x.jpg",
            media_mime="image/jpeg",
        )
    )
    assert msg_id is not None

    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.session_id == phone))
        ).scalar_one()
        conv.status = ConversationStatus.bot
        conv.spam_reviewed = True
        await db.commit()

    await message_buffer.push_message(phone, str(msg_id))
    run = AsyncMock(return_value="He recibido tu imagen, ¿qué necesitas?")
    entregadas = AsyncMock(return_value=1)

    with patch.object(conv_mod, "run_agent", run), \
        patch.object(conv_mod, "deliver_response_parts", entregadas), \
        patch.object(conv_mod, "is_channel_paused", AsyncMock(return_value=False)), \
        patch.object(conv_mod, "is_channel_training", AsyncMock(return_value=False)), \
        patch.object(conv_mod, "moderate", AsyncMock(return_value=SimpleNamespace(flagged=False, categories=[]))), \
        patch.object(conv_mod, "can_call_llm", AsyncMock(return_value=True)), \
        patch.object(message_buffer, "should_process", AsyncMock(return_value=True)):
        await conv_mod.process_buffered_messages(phone)

    run.assert_awaited_once()
    assert "imagen" in run.await_args.kwargs["user_message"], (
        "el agente no se enteró de que el cliente había mandado una imagen"
    )
    entregadas.assert_awaited_once()
