"""La ficha del cliente de WhatsApp se identifica por BSUID, no por teléfono.

EL CAMBIO DE META (abril de 2026). Cuando el cliente tiene nombre de usuario de
WhatsApp, Meta deja de mandar su teléfono, y solo lo revela durante los 30 días
siguientes a cada contacto. Lo que manda SIEMPRE es el BSUID
(Business-Scoped User ID), que además no cambia aunque el cliente se cambie de
número.

QUÉ PASABA. La ficha se buscaba solo por `telefono`. El mismo cliente acababa
con dos: una con `+34600111222` de cuando Meta daba el número y otra con
`wa:ES.13491…` de cuando dejó de darlo. Dos personas distintas para el panel, el
historial partido, y el agente contestando sin saber nada de la conversación de
la semana pasada. Deshacerlo era ir a mano al botón de fusionar.

Lo que se comprueba aquí:
  - el BSUID se guarda y se usa para encontrar la ficha;
  - cuando Meta revela el teléfono, la ficha se actualiza — NO nace otra;
  - cuando pasan los 30 días y vuelve a llegar solo el BSUID, el teléfono bueno
    NO se pisa;
  - si ya había dos fichas, se fusionan solas y no se pierde nada;
  - la fusión (a mano o sola) conserva el BSUID, o volvería a partirse en dos
    al mensaje siguiente;
  - los canales sin BSUID (correo, web) se comportan exactamente igual que
    antes.
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


def _bsuid() -> str:
    return f"ES.{uuid.uuid4().int % 10**20:020d}"


def _telefono() -> str:
    return f"+3462{uuid.uuid4().int % 10_000_000:07d}"


async def _entra_whatsapp(*, telefono: str | None, bsuid: str | None, texto="hola"):
    """Un WhatsApp por la puerta normal. Devuelve el id del mensaje."""
    from app.providers.whatsapp.base import WA_USER_PREFIX, IncomingMessage
    from app.services.conversation import store_incoming

    identificador = telefono or f"{WA_USER_PREFIX}{bsuid}"
    return await store_incoming(
        IncomingMessage(
            provider_message_id=f"wamid.{uuid.uuid4().hex}",
            from_phone=identificador,
            to_phone="+34900000000",
            message_type="text",
            text=texto,
            from_user_id=bsuid,
        )
    )


async def _ficha_de(msg_id):
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.contact import Contact
    from app.models.conversation import Conversation
    from app.models.message import Message

    async with db_session() as db:
        conv_id = (
            await db.execute(select(Message.conversation_id).where(Message.id == msg_id))
        ).scalar_one()
        contact_id = (
            await db.execute(
                select(Conversation.contact_id).where(Conversation.id == conv_id)
            )
        ).scalar_one()
        return (
            await db.execute(select(Contact).where(Contact.id == contact_id))
        ).scalar_one()


async def _cuantas_fichas(bsuid: str) -> int:
    from sqlalchemy import func, select

    from app.db.session import db_session
    from app.models.contact import Contact

    async with db_session() as db:
        return (
            await db.execute(
                select(func.count()).select_from(Contact).where(Contact.wa_user_id == bsuid)
            )
        ).scalar_one()


async def _mensajes_del_contacto(contact_id) -> int:
    from sqlalchemy import func, select

    from app.db.session import db_session
    from app.models.conversation import Conversation
    from app.models.message import Message

    async with db_session() as db:
        return (
            await db.execute(
                select(func.count())
                .select_from(Message)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Conversation.contact_id == contact_id)
            )
        ).scalar_one()


# --------------------------------------------------------------- puros


def test_un_telefono_de_verdad_gana_siempre_al_identificador():
    """Meta solo revela el número 30 días: pisarlo con `wa:` sería perderlo."""
    from app.services.conversation import _mejor_identificador

    assert _mejor_identificador("wa:ES.123", "+34600111222") == "+34600111222"
    # Pasados los 30 días vuelve a llegar solo el BSUID: no se toca el bueno.
    assert _mejor_identificador("+34600111222", "wa:ES.123") == "+34600111222"
    # Cambio de número: el BSUID es el mismo, así que el nuevo es EL suyo.
    assert _mejor_identificador("+34600111222", "+34611000999") == "+34611000999"
    assert _mejor_identificador("wa:ES.123", "") == "wa:ES.123"


# ------------------------------------------------------------------ con BD


@needs_db
@pytest.mark.asyncio
async def test_el_cliente_sin_telefono_entra_y_su_llave_se_guarda():
    bsuid = _bsuid()
    msg_id = await _entra_whatsapp(telefono=None, bsuid=bsuid)
    assert msg_id is not None

    ficha = await _ficha_de(msg_id)
    assert ficha.wa_user_id == bsuid, "el BSUID llegaba y se tiraba"
    assert ficha.telefono == f"wa:{bsuid}"


@needs_db
@pytest.mark.asyncio
async def test_cuando_meta_revela_el_telefono_no_nace_otra_ficha():
    bsuid = _bsuid()
    telefono = _telefono()

    primero = await _entra_whatsapp(telefono=None, bsuid=bsuid)
    ficha_a = await _ficha_de(primero)

    segundo = await _entra_whatsapp(telefono=telefono, bsuid=bsuid, texto="sigo aquí")
    ficha_b = await _ficha_de(segundo)

    assert ficha_a.id == ficha_b.id, "el mismo cliente acabó con dos fichas"
    assert ficha_b.telefono == telefono, "la ficha se quedó con el `wa:` teniendo el número"
    assert await _cuantas_fichas(bsuid) == 1


@needs_db
@pytest.mark.asyncio
async def test_pasados_los_30_dias_el_telefono_bueno_no_se_pisa():
    bsuid = _bsuid()
    telefono = _telefono()

    await _entra_whatsapp(telefono=telefono, bsuid=bsuid)
    # Vuelve meses después: Meta ya no manda el número.
    ultimo = await _entra_whatsapp(telefono=None, bsuid=bsuid, texto="oye")

    ficha = await _ficha_de(ultimo)
    assert ficha.telefono == telefono, (
        "se perdió el teléfono del cliente al pisarlo con el identificador"
    )
    assert ficha.wa_user_id == bsuid


@needs_db
@pytest.mark.asyncio
async def test_a_una_ficha_que_ya_existia_se_le_apunta_la_llave():
    """Fichas de antes del cambio, de una importación o del CRM."""
    bsuid = _bsuid()
    telefono = _telefono()

    viejo = await _entra_whatsapp(telefono=telefono, bsuid=None)
    assert (await _ficha_de(viejo)).wa_user_id is None

    nuevo = await _entra_whatsapp(telefono=telefono, bsuid=bsuid, texto="soy yo")
    ficha = await _ficha_de(nuevo)
    assert ficha.wa_user_id == bsuid, (
        "sin apuntarle la llave, el próximo mensaje sin teléfono abriría otra ficha"
    )


@needs_db
@pytest.mark.asyncio
async def test_las_dos_fichas_del_mismo_cliente_se_unen_solas():
    bsuid = _bsuid()
    telefono = _telefono()

    # Así se partía: primero escribió con nombre de usuario…
    con_usuario = await _entra_whatsapp(telefono=None, bsuid=bsuid, texto="uno")
    ficha_usuario = await _ficha_de(con_usuario)
    # …y aparte había una ficha suya con teléfono, sin BSUID (importación, CRM,
    # o mensajes de antes del cambio de Meta).
    con_telefono = await _entra_whatsapp(telefono=telefono, bsuid=None, texto="dos")
    ficha_telefono = await _ficha_de(con_telefono)
    assert ficha_usuario.id != ficha_telefono.id

    # Ahora Meta manda las dos cosas a la vez: son la misma persona.
    juntas = await _entra_whatsapp(telefono=telefono, bsuid=bsuid, texto="tres")
    superviviente = await _ficha_de(juntas)

    assert superviviente.id == ficha_telefono.id, (
        "sobrevive la ficha del identificador con el que escribe hoy"
    )
    assert superviviente.wa_user_id == bsuid, (
        "sin heredar la llave, el siguiente mensaje sin teléfono volvería a partirla"
    )
    assert await _cuantas_fichas(bsuid) == 1
    # Y el historial de la ficha absorbida está entero en la que queda.
    assert await _mensajes_del_contacto(superviviente.id) == 3

    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.contact import Contact

    async with db_session() as db:
        assert (
            await db.execute(select(Contact).where(Contact.id == ficha_usuario.id))
        ).scalar_one_or_none() is None, "la ficha absorbida sigue viva"


@needs_db
@pytest.mark.asyncio
async def test_el_cambio_de_numero_no_parte_al_cliente_en_dos():
    """El BSUID no cambia aunque el cliente se cambie de número."""
    bsuid = _bsuid()
    viejo, nuevo = _telefono(), _telefono()

    antes = await _entra_whatsapp(telefono=viejo, bsuid=bsuid, texto="desde el viejo")
    ficha_antes = await _ficha_de(antes)

    despues = await _entra_whatsapp(telefono=nuevo, bsuid=bsuid, texto="me he cambiado")
    ficha_despues = await _ficha_de(despues)

    assert await _cuantas_fichas(bsuid) == 1
    assert ficha_despues.telefono == nuevo
    # Da igual cuál de las dos filas sobreviva: lo que no puede pasar es que se
    # pierda el histórico.
    assert await _mensajes_del_contacto(ficha_despues.id) == 2
    assert ficha_antes.wa_user_id == bsuid


@needs_db
@pytest.mark.asyncio
async def test_los_canales_sin_bsuid_se_comportan_igual_que_siempre():
    from app.providers.whatsapp.base import IncomingMessage
    from app.services.conversation import store_incoming

    direccion = f"quien.sea.{uuid.uuid4().hex[:8]}@example.com"
    msg_id = await store_incoming(
        IncomingMessage(
            provider_message_id=f"gmail-{uuid.uuid4()}",
            from_phone=f"email:{direccion}",
            to_phone="email:negocio@example.com",
            message_type="text",
            text="Buenos días",
            raw={"threadId": f"hilo-{uuid.uuid4()}", "subject": "Consulta"},
        )
    )
    ficha = await _ficha_de(msg_id)
    assert ficha.wa_user_id is None
    assert ficha.telefono == f"email:{direccion}"


@needs_db
@pytest.mark.asyncio
async def test_la_fusion_a_mano_tampoco_pierde_la_llave():
    """Fusionar y perder el BSUID deshacía el arreglo al mensaje siguiente."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.contact import Contact
    from app.services.contact_merge import fusionar_contactos

    bsuid = _bsuid()
    con_usuario = await _ficha_de(await _entra_whatsapp(telefono=None, bsuid=bsuid))
    con_telefono = await _ficha_de(await _entra_whatsapp(telefono=_telefono(), bsuid=None))

    async with db_session() as db:
        destino = (
            await db.execute(select(Contact).where(Contact.id == con_telefono.id))
        ).scalar_one()
        origen = (
            await db.execute(select(Contact).where(Contact.id == con_usuario.id))
        ).scalar_one()
        resumen = await fusionar_contactos(db, destino, origen)
        await db.commit()

    assert resumen["bsuid_heredado"] is True
    async with db_session() as db:
        superviviente = (
            await db.execute(select(Contact).where(Contact.id == con_telefono.id))
        ).scalar_one()
        assert superviviente.wa_user_id == bsuid


# ---------------------------------------------------------------------------
# Número reciclado: dos personas distintas peleándose por el mismo teléfono.
#
# El caso: un cliente se da de baja, meses después la operadora reasigna su
# número a otra persona, y esa otra persona escribe al negocio. Los dos son
# clientes reales con su propio identificador de Meta. Fusionarlos sería mezclar
# a dos personas y BORRAR la ficha de una. Irreversible, y sin que nadie se
# entere.
# ---------------------------------------------------------------------------


@needs_db
@pytest.mark.asyncio
async def test_un_numero_reciclado_no_fusiona_a_dos_personas():
    bsuid_a, bsuid_b = _bsuid(), _bsuid()
    telefono = _telefono()

    # Persona A: ficha con su teléfono y su identificador de Meta.
    ficha_a = await _ficha_de(await _entra_whatsapp(telefono=telefono, bsuid=bsuid_a))
    assert ficha_a.wa_user_id == bsuid_a

    # Persona B: entra con nombre de usuario, sin teléfono.
    ficha_b = await _ficha_de(await _entra_whatsapp(telefono=None, bsuid=bsuid_b))
    assert ficha_b.id != ficha_a.id

    # B escribe y Meta revela su teléfono: el que era de A.
    ultimo = await _ficha_de(
        await _entra_whatsapp(telefono=telefono, bsuid=bsuid_b, texto="soy otro")
    )

    # El mensaje es de B, y la ficha de A sigue viva y sin tocar.
    assert ultimo.id == ficha_b.id, "el mensaje acabó en la ficha de otra persona"
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.contact import Contact

    async with db_session() as db:
        a = (
            await db.execute(select(Contact).where(Contact.id == ficha_a.id))
        ).scalar_one_or_none()
        assert a is not None, "se borró la ficha de una persona que no tenía nada que ver"
        assert a.wa_user_id == bsuid_a, "se le cruzó la identidad a la ficha de A"
        assert a.telefono == telefono


@needs_db
@pytest.mark.asyncio
async def test_un_numero_reciclado_sin_ficha_previa_del_nuevo_abre_la_suya():
    """B no tenía ficha: se le abre por su identificador, no se le cuelga la de A."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.contact import Contact

    bsuid_a, bsuid_b = _bsuid(), _bsuid()
    telefono = _telefono()

    ficha_a = await _ficha_de(await _entra_whatsapp(telefono=telefono, bsuid=bsuid_a))
    nueva = await _ficha_de(
        await _entra_whatsapp(telefono=telefono, bsuid=bsuid_b, texto="hola, soy nuevo")
    )

    assert nueva.id != ficha_a.id, "el mensaje se guardó en la ficha del dueño anterior"
    assert nueva.wa_user_id == bsuid_b
    assert nueva.telefono == f"wa:{bsuid_b}", (
        "el teléfono lo tiene la otra ficha: esta se abre por su identificador"
    )
    async with db_session() as db:
        a = (
            await db.execute(select(Contact).where(Contact.id == ficha_a.id))
        ).scalar_one()
        assert a.wa_user_id == bsuid_a, "se le pisó la llave a la ficha de A"


@needs_db
@pytest.mark.asyncio
async def test_la_fusion_automatica_coge_el_cerrojo_de_las_dos_fichas():
    """Sin cerrojo, una ingesta en paralelo podía crear una conversación
    apuntando a la ficha que aquí se borra, y el borrado se la llevaba con sus
    mensajes dentro."""
    import inspect

    from app.services import contact_merge

    fuente = inspect.getsource(contact_merge.fusionar_contactos)
    assert "with_for_update" in fuente
    # Y en orden estable, o dos fusiones simultáneas se abrazan.
    assert "sorted(" in fuente
