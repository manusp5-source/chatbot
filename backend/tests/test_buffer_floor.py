"""Suelo del buffer para canales de texto (agrupado de ráfagas).

El agrupado de mensajes en ráfaga solo funciona si el buffer efectivo supera
el hueco entre mensajes seguidos de una persona. Si el canal de texto resuelve
a un agente con buffer muy bajo (el de VOZ por fallback, o uno mal configurado),
Instagram/WhatsApp respondían a cada mensaje por separado. Verificamos que:
  - texto (instagram/whatsapp) nunca baja de MIN_TEXT_BUFFER_SECONDS,
  - la voz conserva su buffer bajo (necesita latencia),
  - un buffer mayor configurado a mano se respeta.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import text


def _db_available() -> bool:
    try:
        async def _check():
            from app.db.session import db_session
            async with db_session() as db:
                await db.execute(text("SELECT 1"))
        asyncio.run(_check())
        return True
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


@pytestmark_db
@pytest.mark.asyncio
async def test_text_channels_get_buffer_floor_voice_does_not():
    from app.core.config import settings
    from app.db.session import db_session
    from app.models.agent import Agent
    from app.services.runtime_config import get_runtime_for_channel

    name = f"BufTest-{uuid.uuid4().hex[:6]}"
    async with db_session() as db:
        # Aislamos: dejamos activos SOLO nuestros dos agentes, uno de texto y
        # uno de voz, ambos con buffer bajísimo. Hacen falta los DOS porque la
        # resolución por canal ya no permite que un agente de voz atienda un
        # canal de texto (ni al revés). Guardamos los que había activos para
        # restaurarlos y no ensuciar otros tests DB-gated.
        prev = (await db.execute(text("SELECT id FROM agents WHERE is_active = true"))).scalars().all()
        await db.execute(text("UPDATE agents SET is_active = false WHERE is_active = true"))
        agent = Agent(
            name=f"{name}-txt", kind="text", prompt_system="x",
            model_name="gpt-5.4-mini", buffer_seconds=1, is_active=True,
        )
        agent_voz = Agent(
            name=f"{name}-voz", kind="voice", prompt_system="x",
            model_name="gpt-5.4-mini", buffer_seconds=1, is_active=True,
        )
        db.add_all([agent, agent_voz])
        await db.commit()
        aid = agent.id
        vid = agent_voz.id

    try:
        floor = settings.MIN_TEXT_BUFFER_SECONDS
        ig = await get_runtime_for_channel("instagram_dm")
        wa = await get_runtime_for_channel("whatsapp")
        voz = await get_runtime_for_channel("retell_voice")
        assert ig.buffer_seconds == floor
        assert wa.buffer_seconds == floor
        assert voz.buffer_seconds == 1  # la voz NO se sube

        # Un buffer mayor configurado a mano se respeta (no lo baja el suelo).
        async with db_session() as db:
            await db.execute(
                text("UPDATE agents SET buffer_seconds = 12 WHERE id = :i"), {"i": str(aid)}
            )
            await db.commit()
        ig2 = await get_runtime_for_channel("instagram_dm")
        assert ig2.buffer_seconds == 12
    finally:
        async with db_session() as db:
            await db.execute(
                text("DELETE FROM agents WHERE id = ANY(:ids)"),
                {"ids": [str(aid), str(vid)]},
            )
            if prev:
                await db.execute(
                    text("UPDATE agents SET is_active = true WHERE id = ANY(:ids)"),
                    {"ids": [str(p) for p in prev]},
                )
            await db.commit()


def _redis_available() -> bool:
    try:
        async def _check():
            from app.core.redis import get_redis
            await get_redis().ping()
        asyncio.run(_check())
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _redis_available(), reason="Redis no disponible")
@pytest.mark.asyncio
async def test_drain_peek_does_not_drain():
    """drain_peek ve la cola sin vaciarla (guard anti doble-respuesta): si hay
    mensajes encolados durante la generación, la respuesta se descarta y el
    drain pendiente debe seguir encontrando la cola intacta."""
    from app.services import message_buffer

    phone = f"ig:peek{uuid.uuid4().hex[:8]}"
    await message_buffer.push_message(phone, "m1")
    await message_buffer.push_message(phone, "m2")

    peeked = await message_buffer.drain_peek(phone)
    assert [str(x) for x in peeked] == ["m1", "m2"] or peeked == [b"m1", b"m2"]

    # La cola sigue intacta: el drain real se lleva los dos.
    drained = await message_buffer.drain_queue(phone)
    assert len(drained) == 2
    # Y tras drenar, peek está vacío.
    assert await message_buffer.drain_peek(phone) == []
