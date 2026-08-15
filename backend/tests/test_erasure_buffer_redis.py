"""El borrado RGPD tiene que limpiar de verdad el buffer de Redis.

El buffer de mensajes dejó de agruparse por identificador de contacto y pasó a
agruparse por CONVERSACIÓN (`msg_buffer:queue:conv:<id>`), pero el borrado por
derecho de supresión seguía intentando las claves del formato viejo: es decir,
ya no borraba nada. Quedaban en Redis identificadores de mensajes de esa
persona en una cola que NO CADUCA (se crea con RPUSH, sin TTL).

Los ids de conversación hay que recogerlos ANTES del borrado en cascada:
después no existen en ninguna parte.
"""
from __future__ import annotations

import asyncio
import uuid

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


def _incoming(phone: str):
    from app.providers.whatsapp.base import IncomingMessage

    return IncomingMessage(
        provider_message_id=f"wamid.{uuid.uuid4().hex}",
        from_phone=phone,
        to_phone="+34900000000",
        message_type="text",
        text="Quiero que borréis mis datos",
    )


def _phone() -> str:
    return f"+3464{uuid.uuid4().int % 10_000_000:07d}"


@needs_db
@pytest.mark.asyncio
async def test_el_borrado_vacia_el_buffer_de_la_conversacion():
    from sqlalchemy import select

    from app.core.redis import get_redis
    from app.db.session import db_session
    from app.models.contact import Contact
    from app.models.conversation import Conversation
    from app.services import data_erasure, message_buffer

    phone = _phone()
    msg_id = await message_id_de(phone)

    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.session_id == phone))
        ).scalar_one()
        contact_id = conv.contact_id
        conv_id = conv.id

    buffer_key = message_buffer.conversation_key(conv_id)
    await message_buffer.push_message(buffer_key, str(msg_id))
    r = get_redis()
    assert await r.exists(f"msg_buffer:queue:{buffer_key}")

    async with db_session() as db:
        report = await data_erasure.erase_contact(db, contact_id)
        await db.commit()
    await data_erasure.finish_erasure(report)

    assert not await r.exists(f"msg_buffer:queue:{buffer_key}"), (
        "quedan ids de mensajes de la persona borrada en una cola sin caducidad"
    )
    assert not await r.exists(f"msg_buffer:last:{buffer_key}")

    # Y el contacto ya no está (el borrado siguió su curso).
    async with db_session() as db:
        assert (
            await db.execute(select(Contact).where(Contact.id == contact_id))
        ).scalar_one_or_none() is None


async def message_id_de(phone: str):
    from app.services.conversation import store_incoming

    msg_id = await store_incoming(_incoming(phone))
    assert msg_id is not None
    return msg_id


@needs_db
@pytest.mark.asyncio
async def test_el_informe_lleva_las_conversaciones_pero_no_las_enseña():
    """Los ids viajan en el informe SOLO para la limpieza posterior: no pueden
    acabar en el registro de auditoría (que filtra las claves con `_`)."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation
    from app.services import data_erasure

    phone = _phone()
    await message_id_de(phone)
    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.session_id == phone))
        ).scalar_one()
        contact_id, conv_id = conv.contact_id, conv.id

    async with db_session() as db:
        report = await data_erasure.erase_contact(db, contact_id)
        await db.commit()

    assert report["_conversations_for_redis_cleanup"] == [str(conv_id)]
    devuelto = await data_erasure.finish_erasure(report)
    assert "_conversations_for_redis_cleanup" not in devuelto
    assert "_phone_for_redis_cleanup" not in devuelto


@pytest.mark.asyncio
async def test_sin_conversaciones_no_toca_redis():
    """Un contacto sin conversaciones no puede acabar llamando a `DELETE` sin
    claves (redis devuelve error con la lista vacía)."""
    from app.services import data_erasure

    assert await data_erasure._delete_redis_buffer_keys_for_conversations([]) == 0
    assert await data_erasure._delete_redis_buffer_keys_for_conversations(None) == 0


@pytest.mark.asyncio
async def test_se_borran_las_dos_claves_de_cada_conversacion():
    from app.services import data_erasure

    ids = [uuid.uuid4(), uuid.uuid4()]
    assert await data_erasure._delete_redis_buffer_keys_for_conversations(ids) == 4


def test_el_provider_de_voz_expone_el_borrado_de_grabaciones():
    """El borrado de las grabaciones se engancha buscando la función POR NOMBRE
    en el provider. Si allí se renombra, el borrado RGPD deja de llamarla y el
    informe se limita a avisar de que quedan pendientes: mejor que falle aquí."""
    from app.providers.voice import retell

    assert callable(getattr(retell, "delete_call", None))
