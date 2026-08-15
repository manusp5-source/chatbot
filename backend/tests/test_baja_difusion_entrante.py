"""Quien contesta "BAJA" o "STOP" queda fuera de las difusiones, solo.

El opt-out permanente (tabla `outbound_optout`) ya se respetaba en todo envío
masivo, pero nadie lo escribía desde los mensajes ENTRANTES: la persona
contestaba BAJA a una campaña, alguien tenía que verlo en la bandeja y darla de
baja a mano, y mientras tanto seguía recibiendo la siguiente.

El disparador vive en `store_incoming` y va DENTRO de la transacción del
mensaje: o se guardan las dos cosas o ninguna.
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


def _incoming(phone: str, texto: str):
    from app.providers.whatsapp.base import IncomingMessage

    return IncomingMessage(
        provider_message_id=f"wamid.{uuid.uuid4().hex}",
        from_phone=phone,
        to_phone="+34900000000",
        message_type="text",
        text=texto,
    )


def _phone() -> str:
    return f"+3464{uuid.uuid4().int % 10_000_000:07d}"


async def _bajas(phone: str) -> list:
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.outbound_optout import OutboundOptOut

    async with db_session() as db:
        return (
            await db.execute(select(OutboundOptOut).where(OutboundOptOut.phone == phone))
        ).scalars().all()


@needs_db
@pytest.mark.asyncio
async def test_contestar_baja_da_de_baja_de_las_difusiones():
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.message import Message
    from app.models.outbound_optout import OPTOUT_SOURCE_REPLY
    from app.services.conversation import store_incoming

    phone = _phone()
    msg_id = await store_incoming(_incoming(phone, "BAJA"))
    assert msg_id is not None

    bajas = await _bajas(phone)
    assert len(bajas) == 1, "el cliente pidió la baja y siguió en las campañas"
    assert bajas[0].source == OPTOUT_SOURCE_REPLY
    assert "BAJA" in (bajas[0].reason or "")

    # Y el mensaje se guardó igual: la baja no se come el mensaje del cliente.
    async with db_session() as db:
        msg = (
            await db.execute(select(Message).where(Message.id == msg_id))
        ).scalar_one()
        assert msg.contenido == "BAJA"


@needs_db
@pytest.mark.asyncio
async def test_stop_repetido_no_duplica_ni_revienta():
    from app.services.conversation import store_incoming

    phone = _phone()
    assert await store_incoming(_incoming(phone, "stop")) is not None
    assert await store_incoming(_incoming(phone, "Stop.")) is not None

    assert len(await _bajas(phone)) == 1


@needs_db
@pytest.mark.asyncio
async def test_la_baja_medica_no_da_de_baja_a_nadie():
    """"baja" suelta dentro de una frase NO es una petición de baja."""
    from app.services.conversation import store_incoming

    phone = _phone()
    await store_incoming(_incoming(phone, "Me han dado la baja médica, ¿puedo aplazar?"))

    assert await _bajas(phone) == []


@needs_db
@pytest.mark.asyncio
async def test_el_visitante_web_no_ensucia_la_tabla_de_bajas():
    """Las difusiones son plantillas de WhatsApp: un `web:<uuid>` en la tabla
    de bajas sería una fila que ningún envío mira."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.outbound_optout import OutboundOptOut
    from app.services.conversation import store_incoming

    visitante = f"web:{uuid.uuid4()}"
    await store_incoming(_incoming(visitante, "BAJA"))

    async with db_session() as db:
        filas = (
            await db.execute(
                select(OutboundOptOut).where(OutboundOptOut.phone.like("%" + visitante))
            )
        ).scalars().all()
    assert filas == []
