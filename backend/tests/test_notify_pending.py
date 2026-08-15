"""Tests del aviso "Para hacer" por Web Push (cobertura del disparador).

`conversation_needs_action` debe coincidir con lo que la bandeja muestra como
accionable: último mensaje del cliente sin contestar, borrador de email sin
enviar, sugerencia de Entrenamiento, o derivada a humano — y NUNCA cuando el
bot ya contestó, ni en cuarentena/archivada. La tarea diferida solo notifica
cuando hay acción pendiente.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

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


pytestmark_db = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)

_BASE = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)


async def _mk_contact(nombre="Cliente Test"):
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen

    async with db_session() as db:
        c = Contact(telefono=f"+34{uuid.uuid4().int % 10**9:09d}", nombre=nombre, origen=ContactOrigen.whatsapp)
        db.add(c)
        await db.flush()
        cid = c.id
        await db.commit()
        return cid


async def _mk_conv(contact_id, *, status=None, archived=False, quarantined=False):
    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus

    async with db_session() as db:
        conv = Conversation(
            contact_id=contact_id,
            canal=ConversationCanal.whatsapp,
            session_id=uuid.uuid4().hex,
            status=status or ConversationStatus.bot,
            archived_at=_BASE if archived else None,
            quarantined_at=_BASE if quarantined else None,
        )
        db.add(conv)
        await db.flush()
        cid = conv.id
        await db.commit()
        return cid


async def _mk_msg(conv_id, rol, *, contenido=None, extra=None, offset_s=0):
    from app.db.session import db_session
    from app.models.message import Message

    async with db_session() as db:
        m = Message(
            conversation_id=conv_id,
            rol=rol,
            contenido=contenido,
            extra=extra or {},
            created_at=_BASE + timedelta(seconds=offset_s),
        )
        db.add(m)
        await db.commit()


async def _needs(conv_id) -> bool:
    from app.db.session import db_session
    from app.services.inbox_pending import conversation_needs_action

    async with db_session() as db:
        return await conversation_needs_action(db, conv_id)


@pytestmark_db
@pytest.mark.asyncio
async def test_needs_action_cases():
    from app.models.conversation import ConversationStatus
    from app.models.message import MessageRole

    contact = await _mk_contact()

    # 1) Último mensaje del cliente sin contestar → pendiente.
    c1 = await _mk_conv(contact)
    await _mk_msg(c1, MessageRole.user, contenido="¿Tenéis plazas?", offset_s=0)
    assert await _needs(c1) is True

    # 2) El bot contestó (último = assistant, status bot) → NO pendiente.
    c2 = await _mk_conv(contact)
    await _mk_msg(c2, MessageRole.user, contenido="hola", offset_s=0)
    await _mk_msg(c2, MessageRole.assistant, contenido="¡Hola!", offset_s=10)
    assert await _needs(c2) is False

    # 3) Sugerencia de Entrenamiento (borrador training sin enviar) → pendiente.
    c3 = await _mk_conv(contact)
    await _mk_msg(c3, MessageRole.user, contenido="info", offset_s=0)
    await _mk_msg(
        c3, MessageRole.assistant, contenido="(sugerencia)",
        extra={"is_draft": True, "draft_sent": False, "training": True}, offset_s=10,
    )
    assert await _needs(c3) is True

    # 4) Borrador de email sin enviar → pendiente.
    c4 = await _mk_conv(contact)
    await _mk_msg(
        c4, MessageRole.assistant, contenido="(borrador)",
        extra={"is_draft": True, "draft_sent": False}, offset_s=0,
    )
    assert await _needs(c4) is True

    # 5) Cuarentena → fuera de "Para hacer" aunque el cliente escribiera.
    c5 = await _mk_conv(contact, quarantined=True)
    await _mk_msg(c5, MessageRole.user, contenido="spam", offset_s=0)
    assert await _needs(c5) is False

    # 6) Archivada → fuera de "Para hacer".
    c6 = await _mk_conv(contact, archived=True)
    await _mk_msg(c6, MessageRole.user, contenido="hola", offset_s=0)
    assert await _needs(c6) is False

    # 7) Derivada a humano, último = bot (mensaje puente) → pendiente.
    c7 = await _mk_conv(contact, status=ConversationStatus.humano)
    await _mk_msg(c7, MessageRole.user, contenido="quiero hablar con alguien", offset_s=0)
    await _mk_msg(c7, MessageRole.assistant, contenido="Te paso con el equipo", offset_s=10)
    assert await _needs(c7) is True


@pytestmark_db
@pytest.mark.asyncio
async def test_check_task_notifies_only_when_pending(monkeypatch):
    import app.tasks.notify_pending_check as mod
    from app.models.message import MessageRole

    calls: list[tuple] = []

    async def _fake_notify_pending(conversation_id, title, body, url=None, cooldown_seconds=300):
        calls.append((conversation_id, title, body, url))

    # El task importa notify_pending dentro de _check desde app.services.web_push.
    import app.services.web_push as wp
    monkeypatch.setattr(wp, "notify_pending", _fake_notify_pending)

    contact = await _mk_contact(nombre="Lucía")

    # Pendiente (cliente sin contestar) → notifica.
    pending = await _mk_conv(contact)
    await _mk_msg(pending, MessageRole.user, contenido="¿Horario?", offset_s=0)
    await mod._check(pending)
    assert len(calls) == 1
    assert str(pending) == calls[0][0]
    assert "Lucía" in calls[0][1]
    assert calls[0][3] == f"/inbox?conversation={pending}"

    # No pendiente (bot contestó) → no notifica.
    answered = await _mk_conv(contact)
    await _mk_msg(answered, MessageRole.user, contenido="hola", offset_s=0)
    await _mk_msg(answered, MessageRole.assistant, contenido="¡Hola!", offset_s=10)
    await mod._check(answered)
    assert len(calls) == 1  # sin cambios
