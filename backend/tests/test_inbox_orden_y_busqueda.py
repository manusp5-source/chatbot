"""La bandeja: UN solo orden, decidido aquí, y un buscador que busca de verdad.

Lo que se estaba haciendo mal y estos tests fijan:

1. **El orden se decidía dos veces con criterios distintos.** El backend ponía
   primero las derivadas a humano y luego por fecha; el frontend volvía a
   ordenar poniendo primero las que necesitan acción. Y como el backend PAGINA
   con su criterio, una conversación urgente que cayera en el puesto 51 no
   llegaba nunca al frontend: no podía subirla porque no la tenía. Ahora el
   criterio se pide con `sort` y se aplica ANTES de paginar.

2. **El buscador mentía.** Decía "Buscar mensajes, contactos…" y solo miraba
   nombre, teléfono y email del contacto. Ni el texto de los mensajes, ni el
   asunto del correo, ni el @usuario de Instagram que la propia bandeja pinta.

3. **Los comodines de SQL iban sueltos.** Escribir `%` devolvía toda la bandeja.
   El buscador de Contactos ya lo escapaba; el de la bandeja no.

4. **`mark_read` no comprobaba que la conversación existiera**: respondía "ok"
   a cualquier id inventado.

Los endpoints se llaman como funciones normales con una sesión real (mismo
patrón que test_contact_notes.py), sin levantar el stack HTTP.
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


# ---------------------------------------------------------------------------
# Siembra
# ---------------------------------------------------------------------------


async def _list(db, user, **kwargs):
    """Llama a `list_conversations` como función normal.

    El endpoint declara sus filtros con `= Query(...)`, así que al invocarlo sin
    pasar por FastAPI los que no se pasen llegan como objetos `Query` en vez de
    como sus valores. Este envoltorio rellena los que no interesan a cada test
    con lo que serían sus valores por defecto de verdad.
    """
    from app.api.conversations import list_conversations

    defaults = dict(
        status_filter=None,
        canal_filter=None,
        archived="hide",
        quarantine="hide",
        active_only=False,
        unread_only=False,
        pending_only=False,
        suggestions_only=False,
        action_only=False,
        search=None,
        sort="recent",
        page=1,
        page_size=20,
    )
    defaults.update(kwargs)
    return await list_conversations(db=db, _=user, **defaults)


async def _seed_user():
    from app.db.session import db_session
    from app.models.user import User, UserRole

    async with db_session() as db:
        user = User(
            email=f"inbox-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="x",
            role=UserRole.admin,
            nombre="Operadora Test",
        )
        db.add(user)
        await db.flush()
        uid = user.id
        await db.commit()
    async with db_session() as db:
        from sqlalchemy import select

        return (await db.execute(select(User).where(User.id == uid))).scalar_one()


async def _seed_conv(
    *,
    nombre: str,
    minutos: int,
    status=None,
    texto_ultimo: str | None = None,
    rol_ultimo=None,
    subject: str | None = None,
    social_handle: str | None = None,
    email: str | None = None,
    telefono: str | None = None,
    canal=None,
):
    """Crea contacto + conversación + un mensaje, y devuelve (contact_id, conv_id).

    `minutos` = hace cuántos minutos fue el último mensaje (para el orden).
    """
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus
    from app.models.message import Message, MessageRole

    ahora = datetime.now(timezone.utc)
    cuando = ahora - timedelta(minutes=minutos)
    async with db_session() as db:
        contact = Contact(
            telefono=telefono or f"+34{uuid.uuid4().int % 10**9:09d}",
            nombre=nombre,
            email=email,
            social_handle=social_handle,
            origen=ContactOrigen.manual,
        )
        db.add(contact)
        await db.flush()
        conv = Conversation(
            contact_id=contact.id,
            session_id=f"test-{uuid.uuid4().hex[:12]}",
            canal=canal or ConversationCanal.whatsapp,
            status=status or ConversationStatus.bot,
            started_at=cuando,
            last_message_at=cuando,
            subject=subject,
        )
        db.add(conv)
        await db.flush()
        db.add(
            Message(
                conversation_id=conv.id,
                rol=rol_ultimo or MessageRole.assistant,
                contenido=texto_ultimo or "hola",
                created_at=cuando,
            )
        )
        cid, convid = contact.id, conv.id
        await db.commit()
        return cid, convid


# ---------------------------------------------------------------------------
# 1. El orden
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_sort_recent_devuelve_lo_ultimo_movido_primero():
    from app.db.session import db_session

    user = await _seed_user()
    marca = uuid.uuid4().hex[:8]
    _, vieja = await _seed_conv(nombre=f"vieja-{marca}", minutos=600)
    _, nueva = await _seed_conv(nombre=f"nueva-{marca}", minutos=1)

    async with db_session() as db:
        page = await _list(db, user, search=marca, sort="recent")

    ids = [i.id for i in page.items]
    assert ids.index(nueva) < ids.index(vieja)


@pytestmark_db
@pytest.mark.asyncio
async def test_sort_waiting_devuelve_lo_que_lleva_mas_esperando_primero():
    """El espejo exacto de `recent`. Es la cola de "no dejar a nadie tirado"."""
    from app.db.session import db_session

    user = await _seed_user()
    marca = uuid.uuid4().hex[:8]
    _, vieja = await _seed_conv(nombre=f"vieja-{marca}", minutos=600)
    _, nueva = await _seed_conv(nombre=f"nueva-{marca}", minutos=1)

    async with db_session() as db:
        page = await _list(db, user, search=marca, sort="waiting")

    ids = [i.id for i in page.items]
    assert ids.index(vieja) < ids.index(nueva)


@pytestmark_db
@pytest.mark.asyncio
async def test_sort_pending_sube_lo_pendiente_aunque_sea_lo_mas_antiguo():
    """EL fallo de fondo, en una línea.

    Una conversación que necesita a una persona pero cuyo último mensaje es el
    MÁS ANTIGUO de todos. Con el orden por fecha se hunde al final de la lista;
    reordenar en el frontend no la rescataba, porque con paginación real ni
    siquiera llegaba. Pidiendo `sort="pending"` sale la primera, y sale la
    primera EN LA PRIMERA PÁGINA — que es lo que importa.
    """
    from app.db.session import db_session
    from app.models.message import MessageRole

    user = await _seed_user()
    marca = uuid.uuid4().hex[:8]
    # La pendiente es la más vieja: el último mensaje es del cliente y nadie
    # contestó.
    _, pendiente = await _seed_conv(
        nombre=f"pendiente-{marca}", minutos=900, rol_ultimo=MessageRole.user
    )
    # Y por delante, varias atendidas y más recientes.
    for i in range(3):
        await _seed_conv(nombre=f"atendida{i}-{marca}", minutos=10 + i)

    async with db_session() as db:
        por_fecha = await _list(db, user, search=marca, sort="recent")
        por_pendiente = await _list(db, user, search=marca, sort="pending")

    # Por fecha se hunde…
    assert [i.id for i in por_fecha.items][-1] == pendiente
    # …y pidiendo "pendientes primero" encabeza la lista.
    assert por_pendiente.items[0].id == pendiente
    assert por_pendiente.items[0].needs_action is True


@pytestmark_db
@pytest.mark.asyncio
async def test_el_orden_se_aplica_antes_de_paginar():
    """Lo que hacía invisible a la conversación 51.

    Con `page_size=1`, la primera página tiene que traer la que va primera SEGÚN
    EL CRITERIO PEDIDO. Si el orden se aplicara después de recortar (que es lo
    que hacía el frontend), la pendiente no aparecería jamás.
    """
    from app.db.session import db_session
    from app.models.message import MessageRole

    user = await _seed_user()
    marca = uuid.uuid4().hex[:8]
    _, pendiente = await _seed_conv(
        nombre=f"pendiente-{marca}", minutos=900, rol_ultimo=MessageRole.user
    )
    for i in range(4):
        await _seed_conv(nombre=f"otra{i}-{marca}", minutos=1 + i)

    async with db_session() as db:
        page = await _list(
            db, user, search=marca, sort="pending", page=1, page_size=1
        )

    assert len(page.items) == 1
    assert page.items[0].id == pendiente
    # Y el `total` dice cuántas hay de verdad, para poder paginar.
    assert page.total == 5


@pytestmark_db
@pytest.mark.asyncio
async def test_total_permite_recorrer_todas_las_paginas():
    """Con 6 conversaciones y páginas de 2, se recorren TODAS sin repetidas."""
    from app.db.session import db_session

    user = await _seed_user()
    marca = uuid.uuid4().hex[:8]
    esperadas = set()
    for i in range(6):
        _, cid = await _seed_conv(nombre=f"c{i}-{marca}", minutos=i + 1)
        esperadas.add(cid)

    vistas: list[uuid.UUID] = []
    async with db_session() as db:
        for pagina in (1, 2, 3):
            page = await _list(
                db, user, search=marca, page=pagina, page_size=2
            )
            assert page.total == 6
            vistas.extend(i.id for i in page.items)

    assert len(vistas) == 6
    assert set(vistas) == esperadas


# ---------------------------------------------------------------------------
# 2. El buscador
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_busca_dentro_del_texto_de_los_mensajes():
    """El placeholder dice "Buscar mensajes" y ahora es verdad."""
    from app.db.session import db_session

    user = await _seed_user()
    palabra = f"presupuesto{uuid.uuid4().hex[:6]}"
    _, con_palabra = await _seed_conv(
        nombre="Cliente A", minutos=5, texto_ultimo=f"te paso el {palabra} mañana"
    )
    _, sin_palabra = await _seed_conv(nombre="Cliente B", minutos=5, texto_ultimo="buenas")

    async with db_session() as db:
        page = await _list(db, user, search=palabra)

    ids = [i.id for i in page.items]
    assert con_palabra in ids
    assert sin_palabra not in ids


@pytestmark_db
@pytest.mark.asyncio
async def test_busca_en_el_asunto_del_correo_y_en_el_usuario_de_instagram():
    """Dos campos que la bandeja PINTA y que el buscador no miraba."""
    from app.db.session import db_session
    from app.models.conversation import ConversationCanal

    user = await _seed_user()
    marca = uuid.uuid4().hex[:6]
    _, por_asunto = await _seed_conv(
        nombre="Cliente Email",
        minutos=5,
        canal=ConversationCanal.email,
        subject=f"Factura {marca} de septiembre",
        telefono=f"email:cliente-{marca}@example.com",
    )
    _, por_handle = await _seed_conv(
        nombre="Cliente IG",
        minutos=5,
        canal=ConversationCanal.instagram_dm,
        social_handle=f"@usuario{marca}",
        telefono=f"ig:{marca}",
    )

    async with db_session() as db:
        por_subject = await _list(db, user, search=f"factura {marca}")
        por_arroba = await _list(db, user, search=f"usuario{marca}")

    assert [i.id for i in por_subject.items] == [por_asunto]
    assert [i.id for i in por_arroba.items] == [por_handle]


@pytestmark_db
@pytest.mark.asyncio
async def test_el_porcentaje_no_es_un_comodin():
    """Escribir `%` devolvía TODA la bandeja: el LIKE se comía el comodín.

    Ahora `%` se busca como el carácter que es (y como no hay ningún contacto
    ni mensaje que lo lleve, no devuelve nada). Lo mismo con `_`, que convertía
    cada letra en un comodín.
    """
    from app.db.session import db_session

    user = await _seed_user()
    await _seed_conv(nombre=f"Cliente {uuid.uuid4().hex[:6]}", minutos=5)

    async with db_session() as db:
        todo = await _list(db, user)
        con_comodin = await _list(db, user, search="%")
        con_guion_bajo = await _list(db, user, search="_")

    assert todo.total >= 1
    assert con_comodin.total == 0, "el % se estaba tratando como comodín de SQL"
    assert con_guion_bajo.total == 0, "el _ se estaba tratando como comodín de SQL"


# ---------------------------------------------------------------------------
# 3. La ficha de una conversación
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_get_conversation_dice_si_esta_en_cuarentena():
    """Abierta desde la ficha del contacto parecía una conversación normal.

    `get_conversation` no devolvía `quarantined` ni `quarantine_reason`, así que
    no salía ni el aviso ni el botón de liberarla: solo un bot que, sin motivo
    aparente, no contestaba.
    """
    from sqlalchemy import select

    from app.api.conversations import get_conversation
    from app.db.session import db_session
    from app.models.conversation import Conversation

    user = await _seed_user()
    _, conv_id = await _seed_conv(nombre="Sospechoso", minutos=5, texto_ultimo="compra criptos")

    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()
        conv.quarantined_at = datetime.now(timezone.utc)
        conv.quarantine_reason = "spam"
        await db.commit()

    async with db_session() as db:
        ficha = await get_conversation(conv_id, db=db, _=user)

    assert ficha.quarantined is True
    assert ficha.quarantine_reason == "spam"
    # Y de paso el preview, que la lista sí daba y la ficha no.
    assert ficha.last_message_preview == "compra criptos"


@pytestmark_db
@pytest.mark.asyncio
async def test_mark_read_de_una_conversacion_inexistente_da_404():
    """Antes respondía 200 "ok" a cualquier id: el panel se quedaba tan ancho."""
    from fastapi import HTTPException

    from app.api.conversations import mark_read
    from app.db.session import db_session

    user = await _seed_user()
    async with db_session() as db:
        with pytest.raises(HTTPException) as exc:
            await mark_read(uuid.uuid4(), db=db, _=user)
    assert exc.value.status_code == 404


@pytestmark_db
@pytest.mark.asyncio
async def test_unread_only_separa_lo_no_leido_de_lo_ya_atendido():
    """El filtro existía en el backend y no había forma de pedirlo desde la
    pantalla, así que no se podía distinguir lo que te falta por leer de lo que
    el bot ya contestó."""
    from app.api.conversations import mark_read
    from app.db.session import db_session
    from app.models.message import MessageRole

    user = await _seed_user()
    marca = uuid.uuid4().hex[:8]
    _, sin_leer = await _seed_conv(
        nombre=f"sinleer-{marca}", minutos=5, rol_ultimo=MessageRole.user
    )
    _, leida = await _seed_conv(nombre=f"leida-{marca}", minutos=5, rol_ultimo=MessageRole.user)

    async with db_session() as db:
        await mark_read(leida, db=db, _=user)

    async with db_session() as db:
        page = await _list(db, user, search=marca, unread_only=True)

    ids = [i.id for i in page.items]
    assert sin_leer in ids
    assert leida not in ids
