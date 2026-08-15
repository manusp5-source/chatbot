"""Al cerrar una conversación web, el visitante tiene que enterarse.

Antes, cuando la operadora cerraba desde el panel, el widget del visitante se
quedaba abierto como si nada: al escribir se topaba con un conflicto y lo
trataba como sesión caducada, abriendo una conversación nueva. El widget ya
maneja bien el cierre y escucha `conversation.closed` en su canal; faltaba
publicarlo.

Reglas que se prueban:
  - canal web → se publica `conversation.closed` en el canal DEL VISITANTE
    (`webchat:{conversation_id}`), además del `conversation.updated` de la
    bandeja.
  - otros canales → nada al canal del visitante (no existe tal cosa).
  - si publicar falla, el cierre NO se rompe: ya está confirmado en la BD.
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


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


async def _seed_conversacion(canal):
    """Contacto + conversación abierta del canal dado."""
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus

    suf = uuid.uuid4().hex[:8]
    es_web = canal == ConversationCanal.web
    async with db_session() as db:
        contact = Contact(
            telefono=(f"web:{suf}" if es_web else f"+34{uuid.uuid4().int % 10**9:09d}"),
            origen=(ContactOrigen.web if es_web else ContactOrigen.whatsapp),
            in_crm=False,
        )
        db.add(contact)
        await db.flush()
        conv = Conversation(
            contact_id=contact.id,
            canal=canal,
            session_id=f"sess-{suf}",
            status=ConversationStatus.humano,
        )
        db.add(conv)
        await db.commit()
        return contact.id, conv.id


async def _limpiar(contact_id) -> None:
    from sqlalchemy import text

    from app.db.session import db_session

    async with db_session() as db:
        await db.execute(
            text("DELETE FROM contacts WHERE id = :i"), {"i": str(contact_id)}
        )
        await db.commit()


async def _cerrar(conv_id, publish):
    """Llama al endpoint de cierre con el bus mockeado y devuelve su respuesta."""
    from app.api import conversations as conv_api
    from app.db.session import db_session

    async with db_session() as db:
        with patch.object(conv_api.event_bus, "publish", publish):
            return await conv_api.close_conversation(
                conversation_id=conv_id,
                body=conv_api.CloseBody(resumen="Resuelto por teléfono"),
                db=db,
                _=SimpleNamespace(id=uuid.uuid4()),
            )


def test_al_cerrar_un_chat_web_se_avisa_al_visitante():
    from app.models.conversation import ConversationCanal
    from app.services.channel_sender import webchat_channel

    async def _run():
        contact_id, conv_id = await _seed_conversacion(ConversationCanal.web)
        publish = AsyncMock()
        try:
            await _cerrar(conv_id, publish)
            canales = [c.args[0] for c in publish.await_args_list]
            tipos = [c.args[1] for c in publish.await_args_list]
            assert webchat_channel(conv_id) in canales, (
                "el visitante no recibe nada: su widget se queda abierto"
            )
            assert "conversation.closed" in tipos
            # Y la bandeja del panel se sigue enterando.
            assert "conversation.updated" in tipos

            aviso = next(
                c for c in publish.await_args_list if c.args[1] == "conversation.closed"
            )
            assert aviso.args[0] == webchat_channel(conv_id)
            assert aviso.args[2]["conversation_id"] == str(conv_id)
        finally:
            await _limpiar(contact_id)

    asyncio.run(_run())


def test_en_los_demas_canales_no_se_publica_al_canal_del_visitante():
    from app.models.conversation import ConversationCanal

    async def _run():
        contact_id, conv_id = await _seed_conversacion(ConversationCanal.whatsapp)
        publish = AsyncMock()
        try:
            await _cerrar(conv_id, publish)
            tipos = [c.args[1] for c in publish.await_args_list]
            assert "conversation.closed" not in tipos
            assert tipos == ["conversation.updated"]
        finally:
            await _limpiar(contact_id)

    asyncio.run(_run())


def test_si_falla_el_aviso_el_cierre_no_se_rompe():
    """El cierre ya está confirmado en la BD: un bus caído no puede tumbarlo ni
    dejar a la operadora con un error después de haber cerrado."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus

    async def _run():
        contact_id, conv_id = await _seed_conversacion(ConversationCanal.web)
        publish = AsyncMock(side_effect=RuntimeError("Redis caído"))
        try:
            res = await _cerrar(conv_id, publish)
            assert res.ok is True

            async with db_session() as db:
                conv = (
                    await db.execute(
                        select(Conversation).where(Conversation.id == conv_id)
                    )
                ).scalar_one()
            assert conv.status == ConversationStatus.cerrada
            assert conv.ended_at is not None
            assert conv.resumen == "Resuelto por teléfono"
        finally:
            await _limpiar(contact_id)

    asyncio.run(_run())
