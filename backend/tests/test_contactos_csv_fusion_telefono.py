"""Contactos: el CSV que se duplicaba, la fusión que no existía y el teléfono
que no se podía corregir.

Lo que se estaba haciendo mal:

1. **Exportar e importar duplicaba los contactos.** La exportación antepone una
   comilla a todo valor que empiece por `= + - @` (protección contra fórmulas de
   Excel) y TODO teléfono E.164 empieza por `+`. La importación deduplicaba
   comparando la cadena exacta, así que `'+34600111222` no casaba con
   `+34600111222`: bajarse el fichero, mirarlo en Excel y volver a subirlo —el
   flujo más natural del mundo— creaba una ficha nueva por cada contacto
   internacional.

2. **No se podía fusionar dos fichas del mismo cliente**, y como el
   emparejamiento es por cadena exacta, `+34600111222`, `34600111222` y
   `600 111 222` convivían como tres personas distintas.

3. **El teléfono no se podía corregir**: no estaba en el esquema de
   actualización, así que un número mal tecleado solo se arreglaba borrando la
   ficha, y con ella se iban conversaciones, notas y actividad.

4. **La importación no validaba el email**, no dejaba rastro y hacía una
   consulta por fila.

5. **Renombrar una etiqueta a un nombre existente daba un 500.**
"""
from __future__ import annotations

import asyncio
import io
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


pytestmark_db = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


class _FakeUpload:
    """Lo justo de UploadFile que usa el endpoint de importación."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.filename = "contactos.csv"

    async def read(self) -> bytes:
        return self._data


def _request(path: str = "/contacts/export"):
    """Request de Starlette de verdad: el limitador de peticiones (slowapi)
    exige una instancia real, no un doble. Cada llamada usa una IP distinta para
    no chocar contra el límite por IP del propio endpoint."""
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
            "client": (f"203.0.113.{uuid.uuid4().int % 250 + 1}", 51234),
            "scheme": "https",
            "server": ("api.test", 443),
        }
    )


async def _seed_admin():
    from app.db.session import db_session
    from app.models.user import User, UserRole
    from sqlalchemy import select

    async with db_session() as db:
        user = User(
            email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="x",
            role=UserRole.admin,
            nombre="Admin Test",
        )
        db.add(user)
        await db.flush()
        uid = user.id
        await db.commit()
    async with db_session() as db:
        return (await db.execute(select(User).where(User.id == uid))).scalar_one()


async def _seed_contact(telefono: str, **kwargs):
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen

    async with db_session() as db:
        c = Contact(telefono=telefono, origen=ContactOrigen.manual, **kwargs)
        db.add(c)
        await db.flush()
        cid = c.id
        await db.commit()
        return cid


async def _count_contacts(telefono_like: str) -> int:
    from sqlalchemy import func, select

    from app.db.session import db_session
    from app.models.contact import Contact

    async with db_session() as db:
        return (
            await db.execute(
                select(func.count())
                .select_from(Contact)
                .where(Contact.telefono.like(f"%{telefono_like}%"))
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# 1. El CSV que duplicaba
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_exportar_e_importar_no_duplica_los_telefonos_internacionales():
    """El ciclo completo, tal cual lo haría la dueña.

    Se exporta, se coge el CSV literal que salió (con su `'+34…`) y se vuelve a
    importar. Antes: una ficha nueva por contacto. Ahora: cero creados, todos
    reconocidos como ya existentes.
    """
    from app.api.contacts import export_contacts, import_contacts
    from app.db.session import db_session

    admin = await _seed_admin()
    sufijo = f"{uuid.uuid4().int % 10**6:06d}"
    telefono = f"+34600{sufijo}"
    await _seed_contact(telefono, nombre="Cliente Internacional")

    async with db_session() as db:
        resp = await export_contacts(
            _request(), include_outside_crm=False, db=db, current_user=admin
        )
    csv_texto = resp.body.decode("utf-8")
    # La protección anti-fórmulas SIGUE ahí: es lo que hay que conservar.
    assert f"'{telefono}" in csv_texto, "se ha perdido la protección contra fórmulas"

    antes = await _count_contacts(sufijo)
    async with db_session() as db:
        resultado = await import_contacts(
            _request(),
            file=_FakeUpload(csv_texto.encode("utf-8")),
            db=db,
            current_user=admin,
        )
    despues = await _count_contacts(sufijo)

    assert resultado.created == 0, "el CSV de la propia exportación creó fichas nuevas"
    assert despues == antes, "importar el CSV exportado duplicó contactos"


@pytestmark_db
@pytest.mark.asyncio
async def test_el_telefono_se_normaliza_al_importar():
    """"+34 600 111 222" y "0034600111222" son el MISMO cliente.

    Se importan tres formas del mismo número en un solo fichero: tiene que
    quedar UNA ficha, no tres.
    """
    from app.api.contacts import import_contacts
    from app.db.session import db_session

    admin = await _seed_admin()
    sufijo = f"{uuid.uuid4().int % 10**6:06d}"
    csv_texto = (
        "nombre,telefono\n"
        f"Ana,+34600{sufijo}\n"
        f"Ana bis,+34 600 {sufijo}\n"
        f"Ana tris,0034600{sufijo}\n"
    )

    async with db_session() as db:
        resultado = await import_contacts(
            _request(),
            file=_FakeUpload(csv_texto.encode("utf-8")),
            db=db,
            current_user=admin,
        )

    assert resultado.created == 1
    assert resultado.updated == 2
    assert await _count_contacts(sufijo) == 1


@pytestmark_db
@pytest.mark.asyncio
async def test_la_importacion_rechaza_un_email_invalido_y_dice_por_que():
    """El alta manual validaba el email y la importación no: colaba cualquier
    cosa y luego reventaba al mandar un correo. Y la fila rechazada salía como
    "omitido" a secas, sin motivo."""
    from app.api.contacts import import_contacts
    from app.db.session import db_session

    admin = await _seed_admin()
    sufijo = f"{uuid.uuid4().int % 10**6:06d}"
    csv_texto = (
        "nombre,telefono,email\n"
        f"Buena,+34600{sufijo},ok@example.com\n"
        f"Mala,+34611{sufijo},esto-no-es-un-email\n"
    )

    async with db_session() as db:
        resultado = await import_contacts(
            _request(),
            file=_FakeUpload(csv_texto.encode("utf-8")),
            db=db,
            current_user=admin,
        )

    assert resultado.created == 1
    assert resultado.skipped == 1
    assert any("email" in e.lower() for e in resultado.errors), resultado.errors
    assert any("Fila 3" in e for e in resultado.errors), resultado.errors


@pytestmark_db
@pytest.mark.asyncio
async def test_la_exportacion_saca_lo_mismo_que_enseña_la_pantalla():
    """Sacaba TODOS los contactos, incluidos los que no están en el CRM, mientras
    la pantalla solo cuenta los que sí: el fichero traía más filas de las que la
    pantalla decía y no había forma de saber por qué."""
    from app.api.contacts import export_contacts
    from app.db.session import db_session

    admin = await _seed_admin()
    sufijo = f"{uuid.uuid4().int % 10**6:06d}"
    await _seed_contact(f"+34600{sufijo}", nombre="En el CRM", in_crm=True)
    await _seed_contact(f"+34611{sufijo}", nombre="Fuera del CRM", in_crm=False)

    async with db_session() as db:
        solo_crm = (
            await export_contacts(
                _request(), include_outside_crm=False, db=db, current_user=admin
            )
        ).body.decode("utf-8")
        con_todos = (
            await export_contacts(
                _request(), include_outside_crm=True, db=db, current_user=admin
            )
        ).body.decode("utf-8")

    assert f"+34600{sufijo}" in solo_crm
    assert f"+34611{sufijo}" not in solo_crm, "la exportación sacaba contactos fuera del CRM"
    assert f"+34611{sufijo}" in con_todos


@pytestmark_db
@pytest.mark.asyncio
async def test_el_csv_lleva_los_campos_que_hacen_falta_para_una_copia():
    """Faltaban NIF, dirección, notas y etiquetas: el fichero no servía de copia."""
    import csv as _csv

    from app.api.contacts import export_contacts
    from app.db.session import db_session

    admin = await _seed_admin()
    sufijo = f"{uuid.uuid4().int % 10**6:06d}"
    await _seed_contact(
        f"+34600{sufijo}",
        nombre="Con ficha completa",
        nif="B12345678",
        direccion="Calle Mayor 1",
        notas_internas="Cliente de toda la vida",
    )

    async with db_session() as db:
        texto = (
            await export_contacts(
                _request(), include_outside_crm=False, db=db, current_user=admin
            )
        ).body.decode("utf-8")

    filas = list(_csv.DictReader(io.StringIO(texto.lstrip("﻿"))))
    fila = next(f for f in filas if sufijo in (f.get("telefono") or ""))
    assert fila["nif"] == "B12345678"
    assert fila["direccion"] == "Calle Mayor 1"
    assert fila["notas_internas"] == "Cliente de toda la vida"
    assert "etiquetas" in fila


# ---------------------------------------------------------------------------
# 2. Fusionar fichas
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_fusionar_trae_conversaciones_notas_etiquetas_y_rellena_huecos():
    """Dos fichas de la misma persona se convierten en una sin perder nada."""
    from sqlalchemy import func, select

    from app.api.contacts import ContactMergeBody, merge_contacts
    from app.db.session import db_session
    from app.models.contact import Contact, ContactTag
    from app.models.contact_note import ContactNote
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus
    from app.models.tag import Tag

    admin = await _seed_admin()
    sufijo = f"{uuid.uuid4().int % 10**6:06d}"
    # Destino: tiene nombre pero le falta el email.
    destino_id = await _seed_contact(f"+34600{sufijo}", nombre="Ana Buena")
    # Origen: sin nombre, pero con email, una conversación, una nota y una tag.
    origen_id = await _seed_contact(f"34600{sufijo}", email="ana@example.com")

    async with db_session() as db:
        conv = Conversation(
            contact_id=origen_id,
            session_id=f"test-{uuid.uuid4().hex[:12]}",
            canal=ConversationCanal.whatsapp,
            status=ConversationStatus.bot,
        )
        db.add(conv)
        db.add(ContactNote(contact_id=origen_id, texto="Nota de la ficha duplicada"))
        tag = Tag(nombre=f"etiqueta-{sufijo}", color="#3b82f6")
        db.add(tag)
        await db.flush()
        db.add(ContactTag(contact_id=origen_id, tag_id=tag.id))
        await db.commit()

    async with db_session() as db:
        resultado = await merge_contacts(
            destino_id,
            ContactMergeBody(source_id=origen_id),
            db=db,
            current_user=admin,
        )

    # La ficha que sobrevive conserva lo suyo y hereda lo que le faltaba.
    assert resultado.nombre == "Ana Buena"
    assert resultado.email == "ana@example.com"
    assert [t.nombre for t in resultado.tags] == [f"etiqueta-{sufijo}"]

    async with db_session() as db:
        # La ficha absorbida ya no existe.
        assert (
            await db.execute(select(Contact).where(Contact.id == origen_id))
        ).scalar_one_or_none() is None
        # Y sus conversaciones y notas están ahora en la que sobrevive.
        n_convs = (
            await db.execute(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.contact_id == destino_id)
            )
        ).scalar_one()
        n_notas = (
            await db.execute(
                select(func.count())
                .select_from(ContactNote)
                .where(ContactNote.contact_id == destino_id)
            )
        ).scalar_one()
    assert n_convs == 1
    assert n_notas == 1


@pytestmark_db
@pytest.mark.asyncio
async def test_fusionar_no_pisa_lo_que_la_ficha_buena_ya_tenia():
    """Fusionar no puede EMPEORAR la ficha que se queda."""
    from app.api.contacts import ContactMergeBody, merge_contacts
    from app.db.session import db_session

    admin = await _seed_admin()
    sufijo = f"{uuid.uuid4().int % 10**6:06d}"
    destino_id = await _seed_contact(
        f"+34600{sufijo}", nombre="Nombre bueno", email="bueno@example.com"
    )
    origen_id = await _seed_contact(
        f"34600{sufijo}", nombre="Nombre viejo", email="viejo@example.com"
    )

    async with db_session() as db:
        resultado = await merge_contacts(
            destino_id, ContactMergeBody(source_id=origen_id), db=db, current_user=admin
        )

    assert resultado.nombre == "Nombre bueno"
    assert resultado.email == "bueno@example.com"


@pytestmark_db
@pytest.mark.asyncio
async def test_no_se_puede_fusionar_una_ficha_consigo_misma():
    from fastapi import HTTPException

    from app.api.contacts import ContactMergeBody, merge_contacts
    from app.db.session import db_session

    admin = await _seed_admin()
    cid = await _seed_contact(f"+34600{uuid.uuid4().int % 10**6:06d}")

    async with db_session() as db:
        with pytest.raises(HTTPException) as exc:
            await merge_contacts(
                cid, ContactMergeBody(source_id=cid), db=db, current_user=admin
            )
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# 3. Corregir el teléfono
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_se_puede_corregir_el_telefono_y_queda_registrado():
    """Sin esto, un número mal tecleado obligaba a borrar la ficha entera."""
    from sqlalchemy import select

    from app.api.contacts import ContactUpdate, update_contact
    from app.db.session import db_session
    from app.models.contact_activity import ContactActivity

    admin = await _seed_admin()
    sufijo = f"{uuid.uuid4().int % 10**6:06d}"
    cid = await _seed_contact(f"+34600{sufijo}", nombre="Con número mal")

    async with db_session() as db:
        # Se escribe con espacios, como lo escribiría cualquiera.
        actualizado = await update_contact(
            cid,
            ContactUpdate(telefono=f"+34 611 {sufijo}"),
            db=db,
            current_user=admin,
        )

    # Guardado en E.164, sin espacios.
    assert actualizado.telefono == f"+34611{sufijo}"

    async with db_session() as db:
        tipos = (
            await db.execute(
                select(ContactActivity.tipo).where(ContactActivity.contact_id == cid)
            )
        ).scalars().all()
    assert "phone_changed" in tipos, "cambiar la llave del contacto no dejó rastro"


@pytestmark_db
@pytest.mark.asyncio
async def test_no_se_puede_poner_un_telefono_que_ya_es_de_otra_ficha():
    """Eso no es corregir un número: es querer fusionar. Y para eso está /merge."""
    from fastapi import HTTPException

    from app.api.contacts import ContactUpdate, update_contact
    from app.db.session import db_session

    admin = await _seed_admin()
    sufijo = f"{uuid.uuid4().int % 10**6:06d}"
    uno = await _seed_contact(f"+34600{sufijo}")
    await _seed_contact(f"+34611{sufijo}")

    async with db_session() as db:
        with pytest.raises(HTTPException) as exc:
            await update_contact(
                uno, ContactUpdate(telefono=f"+34611{sufijo}"), db=db, current_user=admin
            )
    assert exc.value.status_code == 409
    assert "fusiona" in exc.value.detail.lower()


@pytestmark_db
@pytest.mark.asyncio
async def test_no_se_toca_el_identificador_de_un_contacto_de_canal():
    """En web, Instagram, email y voz el identificador es la ÚNICA llave: si se
    cambia, la ficha se queda huérfana de sus mensajes futuros y no hay forma de
    recuperarla."""
    from fastapi import HTTPException

    from app.api.contacts import ContactUpdate, update_contact
    from app.db.session import db_session

    admin = await _seed_admin()
    cid = await _seed_contact(f"web:{uuid.uuid4()}")

    async with db_session() as db:
        with pytest.raises(HTTPException) as exc:
            await update_contact(
                cid, ContactUpdate(telefono="+34600111222"), db=db, current_user=admin
            )
    assert exc.value.status_code == 409


@pytestmark_db
@pytest.mark.asyncio
async def test_alta_manual_solo_con_email():
    """No se podía dar de alta a un cliente que solo existe por correo.

    La ficha se abre con la llave `email:<direccion>`, que es la MISMA que usa
    el canal de correo: cuando esa persona escriba, su mensaje cae aquí y no en
    una ficha nueva.
    """
    from app.api.contacts import ContactCreate, create_contact
    from app.db.session import db_session

    admin = await _seed_admin()
    direccion = f"solo-email-{uuid.uuid4().hex[:8]}@example.com"

    async with db_session() as db:
        creado = await create_contact(
            ContactCreate(email=direccion, nombre="Cliente de correo"),
            db=db,
            current_user=admin,
        )

    assert creado.telefono == f"email:{direccion}"
    assert creado.email == direccion


@pytestmark_db
@pytest.mark.asyncio
async def test_alta_manual_normaliza_el_telefono():
    """Se guardaba tal cual lo escribiera quien fuera, así que "600 111 222"
    abría una ficha distinta de "+34600111222" y los mensajes entrantes de ese
    cliente no la encontraban nunca."""
    from app.api.contacts import ContactCreate, create_contact
    from app.db.session import db_session

    admin = await _seed_admin()
    sufijo = f"{uuid.uuid4().int % 10**6:06d}"

    async with db_session() as db:
        creado = await create_contact(
            ContactCreate(telefono=f"+34 600 {sufijo}", nombre="Con espacios"),
            db=db,
            current_user=admin,
        )

    assert creado.telefono == f"+34600{sufijo}"


@pytestmark_db
@pytest.mark.asyncio
async def test_alta_manual_sin_telefono_ni_email_no_cuela():
    from fastapi import HTTPException

    from app.api.contacts import ContactCreate, create_contact
    from app.db.session import db_session

    admin = await _seed_admin()
    async with db_session() as db:
        with pytest.raises(HTTPException) as exc:
            await create_contact(
                ContactCreate(nombre="Sin nada"), db=db, current_user=admin
            )
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# 4. Borrar una nota deja rastro
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_borrar_una_nota_aparece_en_la_actividad():
    """Crear una nota se registraba y borrarla no: una nota podía desaparecer
    sin dejar ni una línea en el sitio donde se mira."""
    from sqlalchemy import select

    from app.api.contacts import (
        ContactNoteCreate,
        create_contact_note,
        delete_contact_note,
    )
    from app.db.session import db_session
    from app.models.contact_activity import ContactActivity

    admin = await _seed_admin()
    cid = await _seed_contact(f"+34600{uuid.uuid4().int % 10**6:06d}")

    async with db_session() as db:
        nota = await create_contact_note(
            cid, ContactNoteCreate(texto="Una nota cualquiera"), db=db, current_user=admin
        )
    async with db_session() as db:
        await delete_contact_note(cid, nota.id, db=db, current_user=admin)

    async with db_session() as db:
        tipos = (
            await db.execute(
                select(ContactActivity.tipo).where(ContactActivity.contact_id == cid)
            )
        ).scalars().all()
    assert "note_added" in tipos
    assert "note_deleted" in tipos


# ---------------------------------------------------------------------------
# 5. Etiquetas
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_renombrar_una_etiqueta_a_una_que_ya_existe_da_409_y_no_500():
    """Reventaba con un 500 al hacer commit (choque de unicidad) y en pantalla
    salía como "algo ha fallado"."""
    from fastapi import HTTPException

    from app.api.tags import TagCreate, TagUpdate, create_tag, update_tag
    from app.db.session import db_session

    admin = await _seed_admin()
    marca = uuid.uuid4().hex[:8]

    async with db_session() as db:
        primera = await create_tag(
            TagCreate(nombre=f"clientes-{marca}", color="#3b82f6"), db=db, _=admin
        )
        await create_tag(
            TagCreate(nombre=f"leads-{marca}", color="#3b82f6"), db=db, _=admin
        )

    async with db_session() as db:
        with pytest.raises(HTTPException) as exc:
            await update_tag(
                primera.id, TagUpdate(nombre=f"leads-{marca}"), db=db, _=admin
            )
    assert exc.value.status_code == 409


@pytestmark_db
@pytest.mark.asyncio
async def test_renombrar_una_etiqueta_a_su_mismo_nombre_no_falla():
    """El caso tonto que una comprobación mal hecha rompería: guardar la etiqueta
    sin cambiarle el nombre (por ejemplo, cambiando solo el color)."""
    from app.api.tags import TagCreate, TagUpdate, create_tag, update_tag
    from app.db.session import db_session

    admin = await _seed_admin()
    nombre = f"misma-{uuid.uuid4().hex[:8]}"

    async with db_session() as db:
        tag = await create_tag(TagCreate(nombre=nombre, color="#3b82f6"), db=db, _=admin)
    async with db_session() as db:
        actualizada = await update_tag(
            tag.id, TagUpdate(nombre=nombre, color="#ff0000"), db=db, _=admin
        )
    assert actualizada.nombre == nombre
    assert actualizada.color == "#ff0000"
