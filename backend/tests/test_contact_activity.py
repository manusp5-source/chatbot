"""Tests del timeline de actividad (auditoría real) de la ficha de contacto.

Modelo nuevo: `ContactActivity` registra acciones sobre un contacto DE AQUÍ EN
ADELANTE. La tabla arranca vacía (el histórico previo no se reconstruye).

Cobertura (DB-gated con skipif, mismo patrón que test_contact_notes.py):
  - crear contacto → registra contact_created (actor = usuario actual).
  - cambiar estado → registra status_changed con meta {from, to}.
  - cambiar a un estado idéntico → NO registra.
  - añadir / quitar etiqueta → registran tag_added / tag_removed con el nombre.
  - añadir nota → registra note_added SIN el texto en meta.
  - GET /activity → lista en orden (más reciente primero) con actor resuelto.
  - cascade: al borrar el contacto se borran sus eventos.

Los endpoints se llaman directamente como funciones (inyectando un AsyncSession
real y el usuario), evitando levantar el stack HTTP/auth completo.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


# ---------------------------------------------------------------------------
# DB-gate (mismo patrón que test_contact_notes.py)
# ---------------------------------------------------------------------------


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
# Helpers de siembra
# ---------------------------------------------------------------------------


async def _seed_user(role):
    from app.db.session import db_session
    from app.models.user import User, UserRole

    async with db_session() as db:
        user = User(
            email=f"user-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="x",
            role=role if isinstance(role, UserRole) else UserRole(role),
            nombre="Operador Test",
        )
        db.add(user)
        await db.flush()
        uid = user.id
        await db.commit()
        return uid


async def _get_user(uid):
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.user import User

    async with db_session() as db:
        return (await db.execute(select(User).where(User.id == uid))).scalar_one()


async def _seed_tag(nombre: str):
    from app.db.session import db_session
    from app.models.tag import Tag

    async with db_session() as db:
        tag = Tag(nombre=nombre, color="#6C7BFF")
        db.add(tag)
        await db.flush()
        tid = tag.id
        await db.commit()
        return tid


async def _activity_rows(cid):
    """Eventos de un contacto, más recientes primero (orden del endpoint)."""
    from sqlalchemy import desc, select

    from app.db.session import db_session
    from app.models.contact_activity import ContactActivity

    async with db_session() as db:
        return (
            await db.execute(
                select(ContactActivity)
                .where(ContactActivity.contact_id == cid)
                .order_by(desc(ContactActivity.created_at))
            )
        ).scalars().all()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_create_contact_records_contact_created():
    """Crear contacto registra un evento contact_created con actor = usuario."""
    from app.api.contacts import ContactCreate, create_contact
    from app.db.session import db_session
    from app.models.user import UserRole

    uid = await _get_user(await _seed_user(UserRole.admin))

    async with db_session() as db:
        out = await create_contact(
            ContactCreate(telefono=f"+34{uuid.uuid4().int % 10**9:09d}"),
            db=db,
            current_user=uid,
        )

    rows = await _activity_rows(out.id)
    assert len(rows) == 1
    assert rows[0].tipo == "contact_created"
    assert rows[0].actor_user_id == uid.id
    # meta lleva el origen (manual), nunca PII.
    assert rows[0].meta == {"origen": "manual"}


@pytestmark_db
@pytest.mark.asyncio
async def test_update_status_records_status_changed_with_from_to():
    """Cambiar el estado registra status_changed con meta {from, to}.
    Cambiar a un estado idéntico NO registra."""
    from app.api.contacts import ContactCreate, ContactUpdate, create_contact, update_contact
    from app.db.session import db_session
    from app.models.contact import ContactEstado
    from app.models.user import UserRole

    user = await _get_user(await _seed_user(UserRole.admin))

    async with db_session() as db:
        contact = await create_contact(
            ContactCreate(telefono=f"+34{uuid.uuid4().int % 10**9:09d}", estado=ContactEstado.contacto),
            db=db,
            current_user=user,
        )

    # Cambio real de estado → registra.
    async with db_session() as db:
        await update_contact(
            contact.id, ContactUpdate(estado=ContactEstado.cliente), db=db, current_user=user
        )

    rows = await _activity_rows(contact.id)
    status_events = [r for r in rows if r.tipo == "status_changed"]
    assert len(status_events) == 1
    assert status_events[0].meta == {"from": "contacto", "to": "cliente"}
    assert status_events[0].actor_user_id == user.id

    # "Cambio" a un estado idéntico → NO registra otro evento.
    async with db_session() as db:
        await update_contact(
            contact.id, ContactUpdate(estado=ContactEstado.cliente), db=db, current_user=user
        )
    rows = await _activity_rows(contact.id)
    assert len([r for r in rows if r.tipo == "status_changed"]) == 1


@pytestmark_db
@pytest.mark.asyncio
async def test_add_remove_tag_records_events_with_name():
    """Añadir y quitar etiqueta registran tag_added / tag_removed con el nombre."""
    from app.api.contacts import ContactCreate, add_tag, create_contact, remove_tag
    from app.db.session import db_session
    from app.models.user import UserRole

    user = await _get_user(await _seed_user(UserRole.admin))
    tag_name = f"VIP-{uuid.uuid4().hex[:8]}"
    tid = await _seed_tag(tag_name)

    async with db_session() as db:
        contact = await create_contact(
            ContactCreate(telefono=f"+34{uuid.uuid4().int % 10**9:09d}"), db=db, current_user=user
        )

    async with db_session() as db:
        await add_tag(contact.id, tid, db=db, current_user=user)
    async with db_session() as db:
        await remove_tag(contact.id, tid, db=db, current_user=user)

    rows = await _activity_rows(contact.id)
    added = [r for r in rows if r.tipo == "tag_added"]
    removed = [r for r in rows if r.tipo == "tag_removed"]
    assert len(added) == 1 and added[0].meta == {"tag": tag_name}
    assert len(removed) == 1 and removed[0].meta == {"tag": tag_name}


@pytestmark_db
@pytest.mark.asyncio
async def test_add_note_records_note_added_without_text():
    """Añadir nota registra note_added SIN el texto en meta (no PII)."""
    from app.api.contacts import (
        ContactCreate,
        ContactNoteCreate,
        create_contact,
        create_contact_note,
    )
    from app.db.session import db_session
    from app.models.user import UserRole

    user = await _get_user(await _seed_user(UserRole.cliente))

    async with db_session() as db:
        contact = await create_contact(
            ContactCreate(telefono=f"+34{uuid.uuid4().int % 10**9:09d}"), db=db, current_user=user
        )

    async with db_session() as db:
        await create_contact_note(
            contact.id, ContactNoteCreate(texto="Dato sensible que NO debe ir al meta"),
            db=db, current_user=user,
        )

    rows = await _activity_rows(contact.id)
    note_events = [r for r in rows if r.tipo == "note_added"]
    assert len(note_events) == 1
    assert note_events[0].actor_user_id == user.id
    # El meta no existe o, si existiera, jamás contiene el texto de la nota.
    assert note_events[0].meta in (None, {})


@pytestmark_db
@pytest.mark.asyncio
async def test_list_activity_endpoint_order_and_actor():
    """GET /activity lista más reciente primero y con el actor resuelto."""
    from app.api.contacts import (
        ContactCreate,
        ContactUpdate,
        create_contact,
        list_contact_activity,
        update_contact,
    )
    from app.db.session import db_session
    from app.models.contact import ContactEstado
    from app.models.user import UserRole

    user = await _get_user(await _seed_user(UserRole.admin))

    async with db_session() as db:
        contact = await create_contact(
            ContactCreate(telefono=f"+34{uuid.uuid4().int % 10**9:09d}", estado=ContactEstado.contacto),
            db=db, current_user=user,
        )
    async with db_session() as db:
        await update_contact(
            contact.id, ContactUpdate(estado=ContactEstado.seguimiento), db=db, current_user=user
        )

    async with db_session() as db:
        events = await list_contact_activity(contact.id, db=db, _=user)

    # Más reciente primero: el status_changed va antes que el contact_created.
    assert [e.tipo for e in events] == ["status_changed", "contact_created"]
    # Actor resuelto en ambos (los dos los hizo el mismo usuario).
    assert all(e.actor is not None and e.actor.id == user.id for e in events)
    assert all(e.actor.role == "admin" for e in events)


@pytestmark_db
@pytest.mark.asyncio
async def test_conversation_started_actor_null_resolves_to_none():
    """Un evento de sistema (actor=null) se lista con actor None en el endpoint."""
    from app.api.contacts import ContactCreate, create_contact, list_contact_activity
    from app.db.session import db_session
    from app.models.user import UserRole
    from app.services.contact_activity import record_activity

    user = await _get_user(await _seed_user(UserRole.admin))

    async with db_session() as db:
        contact = await create_contact(
            ContactCreate(telefono=f"+34{uuid.uuid4().int % 10**9:09d}"), db=db, current_user=user
        )

    # Simulamos el evento de ingesta (sistema, sin actor) vía el helper.
    async with db_session() as db:
        await record_activity(
            db, contact.id, "conversation_started",
            actor_user_id=None, meta={"canal": "whatsapp", "conversation_id": str(uuid.uuid4())},
        )
        await db.commit()

    async with db_session() as db:
        events = await list_contact_activity(contact.id, db=db, _=user)

    conv_events = [e for e in events if e.tipo == "conversation_started"]
    assert len(conv_events) == 1
    assert conv_events[0].actor is None
    assert conv_events[0].meta is not None and conv_events[0].meta.get("canal") == "whatsapp"


@pytestmark_db
@pytest.mark.asyncio
async def test_activity_cascade_on_contact_delete():
    """Al borrar el contacto, sus eventos de actividad se borran (CASCADE)."""
    from sqlalchemy import func, select

    from app.api.contacts import ContactCreate, create_contact
    from app.db.session import db_session
    from app.models.contact import Contact
    from app.models.contact_activity import ContactActivity
    from app.models.user import UserRole

    user = await _get_user(await _seed_user(UserRole.admin))

    async with db_session() as db:
        contact = await create_contact(
            ContactCreate(telefono=f"+34{uuid.uuid4().int % 10**9:09d}"), db=db, current_user=user
        )
    cid = contact.id

    async with db_session() as db:
        count_before = (
            await db.execute(
                select(func.count()).select_from(ContactActivity).where(ContactActivity.contact_id == cid)
            )
        ).scalar_one()
    assert count_before >= 1

    async with db_session() as db:
        c = (await db.execute(select(Contact).where(Contact.id == cid))).scalar_one()
        await db.delete(c)
        await db.commit()

    async with db_session() as db:
        count_after = (
            await db.execute(
                select(func.count()).select_from(ContactActivity).where(ContactActivity.contact_id == cid)
            )
        ).scalar_one()
    assert count_after == 0
