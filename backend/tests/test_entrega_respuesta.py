"""La respuesta del agente llega al cliente, o alguien se entera de que no.

Cubre los agujeros del camino de SALIDA que dejaban al cliente sin respuesta
sin una sola señal:

  1. Sin credenciales, los providers devuelven el centinela "noop" y nadie
     miraba ese valor: se guardaba el mensaje como enviado y la bandeja
     enseñaba la conversación perfectamente contestada.
  2. Cualquier error del proveedor al enviar subía hasta Celery, que
     reintentaba en balde (la cola del buffer ya estaba vacía) y dejaba la
     conversación muda, en `bot`, sin derivar y sin log visible.
  3. El mensaje puente de la derivación ("te paso con el equipo") se enviaba
     SIEMPRE por WhatsApp, así que en web e Instagram no lo recibía nadie.
  4. Los adjuntos del panel salían por WhatsApp mirase el canal o no.
  6. Sin tope de caracteres, un párrafo largo se iba entero y WhatsApp lo
     rechazaba con un 400.
  7. La ventana de 24 h de WhatsApp no se comprobaba en el envío del agente.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Utilidades comunes
# ---------------------------------------------------------------------------


def _db_available() -> bool:
    try:
        import asyncio

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


class _FakeStore:
    """"BD" en memoria con la semántica mínima de transacción."""

    def __init__(self) -> None:
        self.pending: list = []
        self.committed: list = []

    def add(self, obj) -> None:
        self.pending.append(obj)

    def commit(self) -> None:
        self.committed.extend(self.pending)
        self.pending = []


class _FakeSession:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store

    def add(self, obj) -> None:
        self.store.add(obj)

    async def commit(self) -> None:
        self.store.commit()

    async def rollback(self) -> None:
        self.store.pending = []


def _conv(canal: str = "whatsapp"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        canal=SimpleNamespace(value=canal),
        contact_id=uuid.uuid4(),
        last_message_at=None,
    )


# ---------------------------------------------------------------------------
# 1) Envío no confirmado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sin_credenciales_el_envio_no_se_da_por_bueno():
    """El provider devuelve "noop" (ni siquiera llamó a la API) → excepción."""
    from app.models.conversation import ConversationCanal
    from app.services import channel_sender as cs

    conv = SimpleNamespace(
        id=uuid.uuid4(), canal=ConversationCanal.whatsapp, contact_id=uuid.uuid4()
    )
    wa = MagicMock()
    wa.send_text = AsyncMock(return_value="noop")

    class _Result:
        def scalar_one(self):
            return SimpleNamespace(telefono="+34600000000")

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            return _Result()

    with patch.object(cs, "last_inbound_age_hours", AsyncMock(return_value=1.0)), \
        patch("app.db.session.db_session", lambda: _FakeDB()), \
        patch("app.services.channel_sender.get_whatsapp_provider", return_value=wa):
        with pytest.raises(cs.SendNotConfirmed):
            await cs.send_text_to_conversation(conv, "hola")


@pytest.mark.asyncio
async def test_un_envio_no_confirmado_no_se_persiste():
    """`deliver_response_parts` no puede guardar lo que no ha salido."""
    from app.services.channel_sender import SendNotConfirmed
    from app.services.conversation import deliver_response_parts

    store = _FakeStore()

    async def send(_conv, _part):
        raise SendNotConfirmed("faltan credenciales")

    with pytest.raises(SendNotConfirmed):
        await deliver_response_parts(
            _FakeSession(store), _conv(), ["hola"], send=send, pause=0
        )

    assert store.committed == [], "se guardó como enviado algo que nadie recibió"


# ---------------------------------------------------------------------------
# 2) Fallo del proveedor al enviar → derivación a humano
# ---------------------------------------------------------------------------


@needs_db
@pytest.mark.asyncio
async def test_fallo_de_envio_deriva_a_humano():
    """Saldo agotado / 429 / corte de red al enviar: la conversación NO puede
    quedarse muda y en `bot`. Mismo tratamiento que el fallo del modelo."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationStatus
    from app.providers.whatsapp.base import IncomingMessage
    from app.services import message_buffer
    from app.services.conversation import process_buffered_messages, store_incoming
    from tests._seed import ensure_text_agent

    await ensure_text_agent()

    phone = f"+3460{uuid.uuid4().int % 10_000_000:07d}"
    msg_id = await store_incoming(
        IncomingMessage(
            provider_message_id=f"wamid.{uuid.uuid4().hex}",
            from_phone=phone,
            to_phone="+34900000000",
            message_type="text",
            text="¿Tenéis cita esta semana?",
        )
    )
    assert msg_id is not None

    async with db_session() as db:
        conv = (
            await db.execute(
                select(Conversation).where(Conversation.session_id == phone)
            )
        ).scalar_one()
        conv.status = ConversationStatus.bot
        conv.spam_reviewed = True
        await db.commit()
        conv_id = conv.id

    await message_buffer.push_message(phone, str(msg_id))

    with patch("app.services.conversation.run_agent", AsyncMock(return_value="Sí, el martes.")), \
        patch("app.services.conversation.deliver_response_parts", AsyncMock(side_effect=RuntimeError("YCloud 402: sin saldo"))), \
        patch("app.services.conversation.is_channel_paused", AsyncMock(return_value=False)), \
        patch("app.services.conversation.is_channel_training", AsyncMock(return_value=False)), \
        patch("app.services.conversation.moderate", AsyncMock(return_value=SimpleNamespace(flagged=False, categories=[]))), \
        patch("app.services.conversation.can_call_llm", AsyncMock(return_value=True)), \
        patch.object(message_buffer, "should_process", AsyncMock(return_value=True)):
        await process_buffered_messages(phone)

    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()
        assert conv.status == ConversationStatus.humano, (
            "el envío falló y la conversación se quedó en bot: nadie la atiende"
        )


@needs_db
@pytest.mark.asyncio
async def test_si_no_se_entrega_ninguna_parte_tambien_deriva():
    """Ventana de mensajería cerrada en la primera parte: cero entregado."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationStatus
    from app.providers.whatsapp.base import IncomingMessage
    from app.services import message_buffer
    from app.services.conversation import process_buffered_messages, store_incoming
    from tests._seed import ensure_text_agent

    await ensure_text_agent()

    phone = f"+3461{uuid.uuid4().int % 10_000_000:07d}"
    msg_id = await store_incoming(
        IncomingMessage(
            provider_message_id=f"wamid.{uuid.uuid4().hex}",
            from_phone=phone,
            to_phone="+34900000000",
            message_type="text",
            text="Hola",
        )
    )
    async with db_session() as db:
        conv = (
            await db.execute(
                select(Conversation).where(Conversation.session_id == phone)
            )
        ).scalar_one()
        conv.status = ConversationStatus.bot
        conv.spam_reviewed = True
        await db.commit()
        conv_id = conv.id

    await message_buffer.push_message(phone, str(msg_id))

    with patch("app.services.conversation.run_agent", AsyncMock(return_value="Hola, dime.")), \
        patch("app.services.conversation.deliver_response_parts", AsyncMock(return_value=0)), \
        patch("app.services.conversation.is_channel_paused", AsyncMock(return_value=False)), \
        patch("app.services.conversation.is_channel_training", AsyncMock(return_value=False)), \
        patch("app.services.conversation.moderate", AsyncMock(return_value=SimpleNamespace(flagged=False, categories=[]))), \
        patch("app.services.conversation.can_call_llm", AsyncMock(return_value=True)), \
        patch.object(message_buffer, "should_process", AsyncMock(return_value=True)):
        await process_buffered_messages(phone)

    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()
        assert conv.status == ConversationStatus.humano


# ---------------------------------------------------------------------------
# 3) Mensaje puente de la derivación, por canal
# ---------------------------------------------------------------------------


class _CapturaEnvio:
    def __init__(self, boom: Exception | None = None) -> None:
        self.llamadas: list = []
        self.boom = boom

    async def __call__(self, conv, text):
        self.llamadas.append((conv, text))
        if self.boom:
            raise self.boom
        return "ext-1"


class _FakeDbCtx:
    """db_session() de mentira que solo acumula lo añadido."""

    def __init__(self, store: _FakeStore) -> None:
        self.store = store

    async def __aenter__(self):
        return _FakeSession(self.store)

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_puente_de_derivacion_usa_el_canal_de_la_conversacion():
    """En web el identificador es `web:<uuid>`: por WhatsApp no llegaba nada."""
    from app.agents.tools import human_handoff as hh
    from app.models.conversation import ConversationCanal
    from app.services import channel_sender as cs

    store = _FakeStore()
    envio = _CapturaEnvio()
    conv = SimpleNamespace(id=uuid.uuid4(), canal=ConversationCanal.web)
    contact = SimpleNamespace(telefono=f"web:{uuid.uuid4()}", nombre="Visitante")

    with patch.object(cs, "send_text_to_conversation", envio), \
        patch.object(hh, "db_session", lambda: _FakeDbCtx(store)), \
        patch.object(hh, "_get_default_bridge_message", AsyncMock(return_value="Te paso con el equipo.")):
        await hh._send_handoff_bridge_message(conv, contact, None)

    assert len(envio.llamadas) == 1, "no se avisó al visitante de la derivación"
    assert envio.llamadas[0][0] is conv
    assert [m.contenido for m in store.committed] == ["Te paso con el equipo."], (
        "el mensaje puente no quedó en el hilo: la operadora no sabe qué se dijo"
    )


@pytest.mark.asyncio
async def test_puente_de_derivacion_fallido_no_es_mudo():
    """Si el envío falla, la derivación sigue, pero con traza visible."""
    from app.agents.tools import human_handoff as hh
    from app.models.conversation import ConversationCanal
    from app.services import channel_sender as cs

    store = _FakeStore()
    envio = _CapturaEnvio(boom=RuntimeError("Graph API 400"))
    logs: list[dict] = []

    async def _fake_log(**kw):
        logs.append(kw)

    conv = SimpleNamespace(id=uuid.uuid4(), canal=ConversationCanal.instagram_dm)
    contact = SimpleNamespace(telefono=f"ig:{uuid.uuid4().hex}", nombre="IG")

    with patch.object(cs, "send_text_to_conversation", envio), \
        patch.object(hh, "db_session", lambda: _FakeDbCtx(store)), \
        patch.object(hh, "push_runtime_log", _fake_log), \
        patch.object(hh, "_get_default_bridge_message", AsyncMock(return_value="Te paso con el equipo.")):
        await hh._send_handoff_bridge_message(conv, contact, None)

    assert any(log.get("level") == "error" for log in logs), (
        "el fallo del mensaje puente se tragó en silencio"
    )
    assert store.committed == [], "se persistió un mensaje que no salió"


@pytest.mark.asyncio
async def test_puente_de_derivacion_no_envia_correo():
    """Email: el agente NUNCA envía (deja borrador). Se deja traza, no envío."""
    from app.agents.tools import human_handoff as hh
    from app.models.conversation import ConversationCanal
    from app.services import channel_sender as cs

    envio = _CapturaEnvio()
    logs: list[dict] = []

    async def _fake_log(**kw):
        logs.append(kw)

    conv = SimpleNamespace(id=uuid.uuid4(), canal=ConversationCanal.email)
    contact = SimpleNamespace(telefono="email:cliente@example.com", nombre="Cliente")

    with patch.object(cs, "send_text_to_conversation", envio), \
        patch.object(hh, "push_runtime_log", _fake_log):
        await hh._send_handoff_bridge_message(conv, contact, None)

    assert envio.llamadas == []
    assert any(log.get("event") == "handoff.bridge.skipped" for log in logs)


# ---------------------------------------------------------------------------
# 4) Adjuntos: se mira el canal ANTES de tocar el disco
# ---------------------------------------------------------------------------


def _client_and_app():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import conversations

    app = FastAPI()
    app.include_router(conversations.router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False), app


class _RowResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeDbRow:
    def __init__(self, row):
        self._row = row
        self.added: list = []

    async def execute(self, *a, **k):
        return _RowResult(self._row)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass


def test_adjunto_en_instagram_no_sale_por_whatsapp():
    """Antes: 502 diciendo "el proveedor de WhatsApp" en un chat de Instagram,
    y el fichero ya guardado en disco (huérfano)."""
    from app.api.deps import get_current_user
    from app.db.session import get_db
    from app.models.conversation import ConversationCanal, ConversationStatus

    client, app = _client_and_app()
    conv = SimpleNamespace(
        id=uuid.uuid4(),
        canal=ConversationCanal.instagram_dm,
        status=ConversationStatus.bot,
    )
    contact = SimpleNamespace(id=uuid.uuid4(), telefono=f"ig:{uuid.uuid4().hex}")

    async def fake_user():
        return SimpleNamespace(id=uuid.uuid4(), role="admin", activo=True)

    async def fake_db():
        yield _FakeDbRow((conv, contact))

    app.dependency_overrides[get_current_user] = fake_user
    app.dependency_overrides[get_db] = fake_db
    try:
        with patch("app.api.conversations.save_local") as save_local:
            resp = client.post(
                f"/api/v1/conversations/{conv.id}/attachment",
                files={"file": ("foto.jpg", b"1234", "image/jpeg")},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 400, (
        f"el adjunto se intentó enviar por WhatsApp en Instagram ({resp.status_code})"
    )
    assert "instagram_dm" in resp.json()["detail"]
    save_local.assert_not_called()  # ni un fichero huérfano en el volumen


# ---------------------------------------------------------------------------
# 6) Tope de caracteres del mensaje
# ---------------------------------------------------------------------------


def test_ningun_trozo_pasa_del_tope_de_whatsapp():
    """Un párrafo largo sin saltos daba un único trozo de 10.000 caracteres →
    400 de WhatsApp y respuesta perdida por el camino silencioso."""
    from app.services.conversation import MAX_PART_CHARS, _split_response

    texto = " ".join(f"palabra{i}" for i in range(2000))  # ~18 KB, sin \n\n
    partes = _split_response(texto, max_parts=3)

    assert all(len(p) <= MAX_PART_CHARS for p in partes), (
        f"hay trozos por encima del tope: {[len(p) for p in partes]}"
    )
    # Y no se pierde una palabra por el camino.
    assert " ".join(partes).split() == texto.split()


def test_el_troceo_normal_no_cambia():
    """El tope no puede romper el troceo de una respuesta corriente."""
    from app.services.conversation import _split_response

    partes = _split_response("Hola.\n\n¿En qué te ayudo?", max_parts=3)
    assert partes == ["Hola.", "¿En qué te ayudo?"]


# ---------------------------------------------------------------------------
# 7) Ventana de 24 h de WhatsApp en el envío del agente
# ---------------------------------------------------------------------------


@needs_db
@pytest.mark.asyncio
async def test_whatsapp_fuera_de_las_24h_no_envia():
    """Se dispara al liberar una cuarentena vieja: el agente respondía, el
    proveedor lo rechazaba y la respuesta se perdía en silencio."""
    from datetime import datetime, timedelta, timezone

    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal
    from app.models.message import Message, MessageRole
    from app.services import channel_sender as cs

    async with db_session() as db:
        contact = Contact(
            telefono=f"+3462{uuid.uuid4().int % 10_000_000:07d}",
            origen=ContactOrigen.whatsapp,
        )
        db.add(contact)
        await db.flush()
        conv = Conversation(
            contact_id=contact.id,
            canal=ConversationCanal.whatsapp,
            session_id=contact.telefono,
        )
        db.add(conv)
        await db.flush()
        db.add(
            Message(
                conversation_id=conv.id,
                rol=MessageRole.user,
                contenido="hola",
                extra={},
                created_at=datetime.now(timezone.utc) - timedelta(hours=30),
            )
        )
        await db.commit()
        conv_id = conv.id
        canal = conv.canal
        contact_id = contact.id

    conv_ligera = SimpleNamespace(id=conv_id, canal=canal, contact_id=contact_id)
    wa = MagicMock()
    wa.send_text = AsyncMock(return_value="wamid.x")
    with patch("app.services.channel_sender.get_whatsapp_provider", return_value=wa):
        with pytest.raises(cs.MessagingWindowClosed):
            await cs.send_text_to_conversation(conv_ligera, "respuesta tardía")
    wa.send_text.assert_not_called()


@needs_db
@pytest.mark.asyncio
async def test_whatsapp_dentro_de_las_24h_si_envia():
    """El corte no puede romper la respuesta normal, que es el 99% de los casos."""
    from datetime import datetime, timedelta, timezone

    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal
    from app.models.message import Message, MessageRole
    from app.services import channel_sender as cs

    async with db_session() as db:
        contact = Contact(
            telefono=f"+3463{uuid.uuid4().int % 10_000_000:07d}",
            origen=ContactOrigen.whatsapp,
        )
        db.add(contact)
        await db.flush()
        conv = Conversation(
            contact_id=contact.id,
            canal=ConversationCanal.whatsapp,
            session_id=contact.telefono,
        )
        db.add(conv)
        await db.flush()
        db.add(
            Message(
                conversation_id=conv.id,
                rol=MessageRole.user,
                contenido="hola",
                extra={},
                created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            )
        )
        await db.commit()
        conv_ligera = SimpleNamespace(
            id=conv.id, canal=conv.canal, contact_id=contact.id
        )

    wa = MagicMock()
    wa.send_text = AsyncMock(return_value="wamid.ok")
    with patch("app.services.channel_sender.get_whatsapp_provider", return_value=wa):
        ext_id = await cs.send_text_to_conversation(conv_ligera, "respuesta")
    assert ext_id == "wamid.ok"
