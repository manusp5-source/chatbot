"""Que WhatsApp acepte un mensaje no es que el cliente lo tenga.

Meta responde 200 con un `wamid` y minutos después manda un webhook diciendo
que no ha podido entregarlo: número que no existe, cliente que bloqueó a la
empresa, ventana de 24 h cerrada, plantilla pausada. Ese webhook llegaba, se
leía y se tiraba a los logs en vivo —1000 líneas en Redis que se pierden al
reiniciar—. En la bandeja el mensaje seguía apareciendo como enviado, así que
la operadora daba la conversación por contestada y el cliente no tenía nada.

Y en YCloud ni eso: el evento `whatsapp.message.updated` se descartaba entero,
porque el parseo solo miraba los mensajes ENTRANTES.

Lo que se comprueba aquí:
  - las dos formas del payload (Meta y YCloud) se leen bien;
  - el estado se pega al mensaje y sobrevive a que lleguen desordenados;
  - un fallo no lo pisa un `delivered` que llegue tarde;
  - los canales sin acuse (web, correo) no se marcan: un "enviado" que no va a
    avanzar nunca se lee como "pendiente de entregar".
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.api.webhooks import _estado_meta, _motivo_del_fallo
from app.services.delivery_status import (
    ENTREGADO,
    ENVIADO,
    FALLIDO,
    LEIDO,
    _es_ascenso,
    canal_con_acuses,
    marcar_enviado,
    traducir_estado,
)


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


# --------------------------------------------------------------- puros


def test_las_palabras_de_los_dos_proveedores_significan_lo_mismo():
    # YCloud mete un `accepted` que Meta no tiene: para lo que aquí importa
    # ("lo tengo en cola") es lo mismo que `sent`.
    assert traducir_estado("accepted") == ENVIADO
    assert traducir_estado("sent") == ENVIADO
    assert traducir_estado("delivered") == ENTREGADO
    assert traducir_estado("READ") == LEIDO
    assert traducir_estado("failed") == FALLIDO
    assert traducir_estado("lo-que-sea") is None
    assert traducir_estado(None) is None


def test_los_estados_solo_suben_de_escalon():
    """Llegan desordenados: un `sent` puede entrar tras un `delivered`."""
    assert _es_ascenso(None, ENVIADO) is True
    assert _es_ascenso(ENVIADO, ENTREGADO) is True
    assert _es_ascenso(ENTREGADO, LEIDO) is True
    assert _es_ascenso(ENTREGADO, ENVIADO) is False
    assert _es_ascenso(LEIDO, ENTREGADO) is False
    assert _es_ascenso(LEIDO, LEIDO) is False


def test_el_fallo_manda_y_no_lo_pisa_nada():
    assert _es_ascenso(LEIDO, FALLIDO) is True
    assert _es_ascenso(FALLIDO, FALLIDO) is False
    # Un `delivered` tardío no puede borrar un fallo: si Meta dijo que no llegó,
    # eso es lo que la operadora tiene que ver.
    assert _es_ascenso(FALLIDO, ENTREGADO) is False
    assert _es_ascenso(FALLIDO, LEIDO) is False


def test_solo_whatsapp_lleva_acuse():
    from app.models.conversation import ConversationCanal

    assert canal_con_acuses(ConversationCanal.whatsapp) is True
    assert canal_con_acuses(ConversationCanal.web) is False
    assert canal_con_acuses(ConversationCanal.email) is False
    assert canal_con_acuses(ConversationCanal.instagram_dm) is False


def test_marcar_enviado_no_pinta_estado_en_los_canales_sin_acuse():
    from app.models.conversation import ConversationCanal

    wa = marcar_enviado({"sent_by": "operator"}, "wamid.1", ConversationCanal.whatsapp)
    assert wa["delivery_status"] == ENVIADO
    assert wa["sent_by"] == "operator"

    web = marcar_enviado({}, "local-uuid", ConversationCanal.web)
    assert web["provider_message_id"] == "local-uuid"
    assert "delivery_status" not in web, (
        "un estado que no va a avanzar nunca se lee como «pendiente de entregar»"
    )

    # Sin id externo no hay nada que anotar (chat web sin proveedor).
    assert marcar_enviado({"k": 1}, "", ConversationCanal.whatsapp) == {"k": 1}


def test_el_motivo_del_fallo_se_lee():
    assert _motivo_del_fallo("131026", "Message undeliverable") == (
        "Message undeliverable · código 131026"
    )
    assert _motivo_del_fallo("", "") == "sin motivo"


def test_el_payload_de_meta_se_traduce():
    crudo = {
        "id": "wamid.HBg",
        "status": "failed",
        "errors": [
            {
                "code": 131026,
                "title": "Message undeliverable",
                "error_data": {"details": "El número no tiene WhatsApp"},
            }
        ],
    }
    out = _estado_meta(crudo)
    assert out["id"] == "wamid.HBg"
    assert out["status"] == "failed"
    assert out["error_code"] == "131026"
    # `details` explica más que `title`, y es lo que le sirve a la operadora.
    assert out["error_message"] == "El número no tiene WhatsApp"


def test_el_payload_de_ycloud_se_lee_y_lo_demas_se_ignora():
    from app.providers.whatsapp.ycloud import YCloudProvider

    eventos = [
        {
            "type": "whatsapp.message.updated",
            "whatsappMessage": {
                "id": "ycloud-1",
                "wamid": "wamid.HBg",
                "status": "failed",
                "errorCode": "131047",
                "errorMessage": "Re-engagement message",
            },
        },
        # Un entrante no es un estado: no puede colarse aquí.
        {"type": "whatsapp.inbound_message.received", "whatsappInboundMessage": {"id": "x"}},
        # Sin estado no hay nada que anotar.
        {"type": "whatsapp.message.updated", "whatsappMessage": {"id": "y"}},
    ]
    estados = YCloudProvider.parse_statuses(YCloudProvider.__new__(YCloudProvider), eventos)
    assert len(estados) == 1
    assert estados[0]["id"] == "ycloud-1"
    assert estados[0]["wamid"] == "wamid.HBg"
    assert estados[0]["status"] == "failed"
    assert estados[0]["error_code"] == "131047"


# ------------------------------------------------------------------ con BD


async def _mensaje_saliente(pmid: str):
    """Un WhatsApp que hemos enviado nosotros y que el proveedor aceptó."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationCanal
    from app.models.message import Message, MessageRole
    from app.providers.whatsapp.base import IncomingMessage
    from app.services.conversation import store_incoming

    telefono = f"+3461{uuid.uuid4().int % 10_000_000:07d}"
    entrante = await store_incoming(
        IncomingMessage(
            provider_message_id=f"wamid.{uuid.uuid4().hex}",
            from_phone=telefono,
            to_phone="+34900000000",
            message_type="text",
            text="hola",
        )
    )
    async with db_session() as db:
        conv_id = (
            await db.execute(select(Message.conversation_id).where(Message.id == entrante))
        ).scalar_one()
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()
        msg = Message(
            conversation_id=conv_id,
            rol=MessageRole.assistant,
            contenido="Te lo miro y te digo",
            extra=marcar_enviado({}, pmid, ConversationCanal.whatsapp),
        )
        db.add(msg)
        await db.commit()
        await db.refresh(msg)
        assert conv.canal == ConversationCanal.whatsapp
        return msg.id


async def _estado_de(msg_id) -> dict:
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.message import Message

    async with db_session() as db:
        msg = (
            await db.execute(select(Message).where(Message.id == msg_id))
        ).scalar_one()
        return dict(msg.extra or {})


@needs_db
@pytest.mark.asyncio
async def test_un_fallo_de_entrega_queda_pegado_al_mensaje():
    from app.services.delivery_status import registrar_estado_entrega

    pmid = f"wamid.{uuid.uuid4().hex}"
    msg_id = await _mensaje_saliente(pmid)
    assert (await _estado_de(msg_id))["delivery_status"] == ENVIADO

    assert await registrar_estado_entrega(pmid, "failed", detalle="El número no tiene WhatsApp")

    extra = await _estado_de(msg_id)
    assert extra["delivery_status"] == FALLIDO
    assert "no tiene WhatsApp" in extra["delivery_detail"]
    assert extra["delivery_at"]


@needs_db
@pytest.mark.asyncio
async def test_los_estados_desordenados_no_hacen_retroceder_el_mensaje():
    from app.services.delivery_status import registrar_estado_entrega

    pmid = f"wamid.{uuid.uuid4().hex}"
    msg_id = await _mensaje_saliente(pmid)

    assert await registrar_estado_entrega(pmid, "read")
    # El `sent` llega tarde (reintento del proveedor): no puede bajar a enviado.
    assert await registrar_estado_entrega(pmid, "sent") is False
    assert (await _estado_de(msg_id))["delivery_status"] == LEIDO

    # Pero un fallo posterior sí manda.
    assert await registrar_estado_entrega(pmid, "failed", detalle="bloqueado")
    assert (await _estado_de(msg_id))["delivery_status"] == FALLIDO
    # Y ya no se deja pisar.
    assert await registrar_estado_entrega(pmid, "delivered") is False
    assert (await _estado_de(msg_id))["delivery_status"] == FALLIDO


@needs_db
@pytest.mark.asyncio
async def test_un_estado_de_otro_no_revienta_ni_escribe_nada():
    """Webhooks de otra instalación, o de mensajes anteriores a esto."""
    from app.services.delivery_status import registrar_estado_entrega

    assert await registrar_estado_entrega("wamid.que-no-es-nuestro", "failed") is False
    assert await registrar_estado_entrega("", "failed") is False
    # Un estado que no conocemos no se inventa nada.
    pmid = f"wamid.{uuid.uuid4().hex}"
    msg_id = await _mensaje_saliente(pmid)
    assert await registrar_estado_entrega(pmid, "en-el-limbo") is False
    assert (await _estado_de(msg_id))["delivery_status"] == ENVIADO


@needs_db
@pytest.mark.asyncio
async def test_un_mensaje_que_no_llego_pone_la_conversacion_en_para_hacer():
    """El globo rojo no sirve de nada si hay que entrar al hilo para verlo."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.message import Message
    from app.services.delivery_status import registrar_estado_entrega
    from app.services.inbox_pending import conversation_needs_action

    pmid = f"wamid.{uuid.uuid4().hex}"
    msg_id = await _mensaje_saliente(pmid)
    async with db_session() as db:
        conv_id = (
            await db.execute(select(Message.conversation_id).where(Message.id == msg_id))
        ).scalar_one()
        # Contestado y entregado: no hay nada que hacer.
        await registrar_estado_entrega(pmid, "delivered")
        assert await conversation_needs_action(db, conv_id) is False

    await registrar_estado_entrega(pmid, "failed", detalle="el número no existe")
    async with db_session() as db:
        assert await conversation_needs_action(db, conv_id) is True, (
            "el cliente no recibió la respuesta y la bandeja daba el hilo por contestado"
        )


@needs_db
@pytest.mark.asyncio
async def test_el_estado_sale_en_la_ficha_del_mensaje_del_panel():
    """Si no llega al serializador, la operadora sigue sin verlo."""
    from sqlalchemy import select

    from app.api.conversations import _message_out
    from app.db.session import db_session
    from app.models.message import Message
    from app.services.delivery_status import registrar_estado_entrega

    pmid = f"wamid.{uuid.uuid4().hex}"
    msg_id = await _mensaje_saliente(pmid)
    await registrar_estado_entrega(pmid, "failed", detalle="ventana de 24 h cerrada")

    async with db_session() as db:
        msg = (await db.execute(select(Message).where(Message.id == msg_id))).scalar_one()
        out = _message_out(msg)
    assert out.delivery_status == FALLIDO
    assert "24 h" in (out.delivery_detail or "")


@needs_db
@pytest.mark.asyncio
async def test_los_fallos_de_una_difusion_no_inundan_para_hacer():
    """Una campaña a 500 números siempre tiene unos cuantos muertos. Sesenta
    conversaciones en «Para hacer» de golpe, ninguna con nada que hacer, es
    enterrar lo que sí importa."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import ConversationCanal
    from app.models.message import Message, MessageRole
    from app.services.delivery_status import marcar_enviado, registrar_estado_entrega
    from app.services.inbox_pending import conversation_needs_action

    pmid = f"wamid.{uuid.uuid4().hex}"
    base = await _mensaje_saliente(f"wamid.{uuid.uuid4().hex}")
    async with db_session() as db:
        conv_id = (
            await db.execute(select(Message.conversation_id).where(Message.id == base))
        ).scalar_one()
        db.add(
            Message(
                conversation_id=conv_id,
                rol=MessageRole.operator,
                contenido="Plantilla de la campaña",
                extra=marcar_enviado(
                    {"source": "outbound_campaign"}, pmid, ConversationCanal.whatsapp
                ),
            )
        )
        await db.commit()

    await registrar_estado_entrega(pmid, "failed", detalle="el número no existe")
    async with db_session() as db:
        assert await conversation_needs_action(db, conv_id) is False


@needs_db
@pytest.mark.asyncio
async def test_cerrar_la_conversacion_es_la_salida_cuando_no_hay_forma_de_entregar():
    """Si el cliente bloqueó a la empresa, TODO lo que se le envíe va a fallar:
    sin una salida, el hilo se queda en «Para hacer» para siempre."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationStatus
    from app.models.message import Message
    from app.services.delivery_status import registrar_estado_entrega
    from app.services.inbox_pending import conversation_needs_action

    pmid = f"wamid.{uuid.uuid4().hex}"
    msg_id = await _mensaje_saliente(pmid)
    await registrar_estado_entrega(pmid, "failed", detalle="bloqueado")

    async with db_session() as db:
        conv_id = (
            await db.execute(select(Message.conversation_id).where(Message.id == msg_id))
        ).scalar_one()
        assert await conversation_needs_action(db, conv_id) is True
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()
        conv.status = ConversationStatus.cerrada
        await db.commit()
    async with db_session() as db:
        assert await conversation_needs_action(db, conv_id) is False
