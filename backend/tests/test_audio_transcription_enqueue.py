"""Encolado de la transcripción de notas de voz.

El fallo que arregla esto: la transcripción se pedía dentro del flujo del
agente, DESPUÉS de los cortes por conversación en Humano / canal pausado /
cuarentena. Una nota de voz que llegaba a una conversación ya atendida por una
persona no se transcribía nunca, en silencio. Ahora se encola al guardar el
mensaje entrante y el único freno es el interruptor por canal.

Tests puros (sin DB ni Redis reales): se verifica el contrato de
`enqueue_transcription`.
"""
import asyncio
import uuid

import pytest


class _FakeRedis:
    """Solo lo que usa el encolado: SET con nx/ex y GET."""

    def __init__(self, initial: dict[str, str] | None = None):
        self.store: dict[str, str] = dict(initial or {})

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


@pytest.fixture
def enqueue_env(monkeypatch):
    """Parchea Redis, el log del panel y la task Celery. Devuelve (redis, encoladas)."""
    from app.services import agent_pause, audio_processor
    from app.tasks import transcribe_audio as task_mod

    redis = _FakeRedis()
    monkeypatch.setattr(audio_processor, "get_redis", lambda: redis)
    # El flag por canal lo lee agent_pause con su propio get_redis.
    monkeypatch.setattr(agent_pause, "get_redis", lambda: redis)

    async def _noop_log(**kwargs):
        return None

    monkeypatch.setattr(audio_processor, "push_runtime_log", _noop_log)

    enqueued: list[str] = []
    monkeypatch.setattr(
        task_mod.transcribe_audio, "delay", lambda mid: enqueued.append(mid)
    )
    return redis, enqueued


@pytest.mark.asyncio
async def test_enqueues_regardless_of_conversation_state(enqueue_env):
    """El canal manda; el estado de la conversación no entra en esta decisión."""
    from app.services.audio_processor import enqueue_transcription

    _redis, enqueued = enqueue_env
    mid = uuid.uuid4()
    assert await enqueue_transcription(mid, "instagram_dm", origin="store_incoming") is True
    assert enqueued == [str(mid)]


@pytest.mark.asyncio
async def test_second_call_does_not_enqueue_twice(enqueue_env):
    """store_incoming y el drain pueden coincidir: no se paga dos veces."""
    from app.services.audio_processor import enqueue_transcription

    _redis, enqueued = enqueue_env
    mid = uuid.uuid4()
    assert await enqueue_transcription(mid, "whatsapp", origin="store_incoming") is True
    assert await enqueue_transcription(mid, "whatsapp", origin="drain") is False
    assert enqueued == [str(mid)]


@pytest.mark.asyncio
async def test_channel_switch_off_blocks_transcription(enqueue_env):
    """Con el interruptor del canal apagado no se encola nada."""
    from app.services.audio_processor import enqueue_transcription

    redis, enqueued = enqueue_env
    redis.store["agent:transcribe_audio:instagram_dm"] = "0"
    assert await enqueue_transcription(uuid.uuid4(), "instagram_dm", origin="store_incoming") is False
    assert enqueued == []


@pytest.mark.asyncio
async def test_switch_is_per_channel(enqueue_env):
    """Apagar Instagram no apaga WhatsApp."""
    from app.services.audio_processor import enqueue_transcription

    redis, enqueued = enqueue_env
    redis.store["agent:transcribe_audio:instagram_dm"] = "0"
    assert await enqueue_transcription(uuid.uuid4(), "whatsapp", origin="store_incoming") is True
    assert len(enqueued) == 1


@pytest.mark.asyncio
async def test_default_is_on(enqueue_env):
    """Un canal que nunca se ha tocado (sin key en Redis) transcribe."""
    from app.services.agent_pause import (
        get_channels_transcription_state,
        is_channel_transcription_enabled,
    )

    _redis, _enqueued = enqueue_env
    assert await is_channel_transcription_enabled("whatsapp") is True
    state = await get_channels_transcription_state()
    assert all(state.values())


# --------- Integración: el caso real que se rompía (necesita DB) ---------

def _db_available() -> bool:
    try:
        async def _check():
            from sqlalchemy import text

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
async def test_store_incoming_enqueues_audio_of_human_handled_conversation(monkeypatch):
    """Nota de voz en una conversación que ya lleva una persona: se transcribe.

    Es el escenario que fallaba en Instagram: en cuanto contestas una vez desde
    el panel, la conversación pasa a Humano y el flujo del agente corta antes de
    pedir la transcripción. Guardar el mensaje debe encolarla igual.
    """
    from sqlalchemy import select, update

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationStatus
    from app.models.message import Message
    from app.providers.whatsapp import IncomingMessage
    from app.services.conversation import store_incoming
    from app.tasks import transcribe_audio as task_mod

    enqueued: list[str] = []
    monkeypatch.setattr(
        task_mod.transcribe_audio, "delay", lambda mid: enqueued.append(mid)
    )

    customer_id = f"ig:AUD{uuid.uuid4().hex[:10]}"

    def _incoming(pmid: str) -> IncomingMessage:
        return IncomingMessage(
            provider_message_id=pmid,
            from_phone=customer_id,
            to_phone="ig:BIZ",
            message_type="audio",
            text=None,
            audio_url="https://lookaside.fbsbx.com/audio.mp4",
            audio_mime="video/mp4",
            customer_name=None,
            raw={},
        )

    first_id = await store_incoming(_incoming(f"mid.{uuid.uuid4().hex}"))
    assert first_id is not None

    # La conversación pasa a Humano (lo que hace responder desde el panel).
    async with db_session() as db:
        conv_id = (
            await db.execute(select(Message.conversation_id).where(Message.id == first_id))
        ).scalar_one()
        await db.execute(
            update(Conversation)
            .where(Conversation.id == conv_id)
            .values(status=ConversationStatus.humano)
        )
        await db.commit()

    enqueued.clear()
    second_id = await store_incoming(_incoming(f"mid.{uuid.uuid4().hex}"))
    assert second_id is not None
    assert enqueued == [str(second_id)]
