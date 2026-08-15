"""Tests de las notas internas multi-autor de la ficha de contacto.

Modelo nuevo: `Contact.notas_internas` (campo único cifrado, ahora archivado)
se sustituye por una LISTA de `ContactNote` con autor, fecha y texto cifrado.

Cobertura (DB-gated con skipif, mismo patrón que test_email_retention.py):
  - crear nota: autor = usuario actual, texto se persiste cifrado y se lee
    descifrado, author resuelto en la salida.
  - listar: orden por fecha desc (más reciente primero) + autor resuelto.
  - borrar: el autor sí puede; otro usuario NO-admin → 403; un admin sí puede.
  - cascade: al borrar el contacto se borran sus notas.

Los endpoints se llaman directamente como funciones (inyectando un AsyncSession
real y el usuario), evitando levantar el stack HTTP/auth completo.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


# ---------------------------------------------------------------------------
# DB-gate (mismo patrón que test_email_retention.py)
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


async def _seed_contact():
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen

    async with db_session() as db:
        contact = Contact(
            telefono=f"+34{uuid.uuid4().int % 10**9:09d}",
            nombre="Contacto de prueba",
            origen=ContactOrigen.manual,
        )
        db.add(contact)
        await db.flush()
        cid = contact.id
        await db.commit()
        return cid


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_create_note_sets_author_and_encrypts():
    """Crear nota: autor = usuario actual; el texto se guarda cifrado (BYTEA)
    pero se devuelve/lee en claro; el author sale resuelto."""
    from sqlalchemy import select, text

    from app.api.contacts import ContactNoteCreate, create_contact_note
    from app.db.session import db_session
    from app.models.user import UserRole

    cid = await _seed_contact()
    uid = await _seed_user(UserRole.cliente)
    user = await _get_user(uid)

    async with db_session() as db:
        out = await create_contact_note(
            cid, ContactNoteCreate(texto="  Llamar el lunes  "), db=db, current_user=user
        )

    assert out.texto == "Llamar el lunes"  # .strip() aplicado
    assert out.author is not None
    assert out.author.id == uid
    assert out.author.role == "cliente"
    assert out.created_at  # ISO string no vacío

    # En BD el texto está cifrado: la columna cruda no contiene el texto plano.
    async with db_session() as db:
        raw = (
            await db.execute(
                text("SELECT texto FROM contact_note WHERE id = :i").bindparams(i=out.id)
            )
        ).scalar_one()
    assert b"Llamar el lunes" not in bytes(raw)

    # Y el ORM lo descifra al leer.
    from app.models.contact_note import ContactNote

    async with db_session() as db:
        note = (
            await db.execute(select(ContactNote).where(ContactNote.id == out.id))
        ).scalar_one()
    assert note.texto == "Llamar el lunes"


@pytestmark_db
@pytest.mark.asyncio
async def test_list_notes_order_and_author():
    """Listar: más recientes primero y con el autor resuelto."""
    from app.api.contacts import (
        ContactNoteCreate,
        create_contact_note,
        list_contact_notes,
    )
    from app.db.session import db_session
    from app.models.user import UserRole

    cid = await _seed_contact()
    uid = await _seed_user(UserRole.admin)
    user = await _get_user(uid)

    async with db_session() as db:
        await create_contact_note(cid, ContactNoteCreate(texto="primera"), db=db, current_user=user)
    async with db_session() as db:
        await create_contact_note(cid, ContactNoteCreate(texto="segunda"), db=db, current_user=user)
    async with db_session() as db:
        await create_contact_note(cid, ContactNoteCreate(texto="tercera"), db=db, current_user=user)

    async with db_session() as db:
        notes = await list_contact_notes(cid, db=db, _=user)

    textos = [n.texto for n in notes]
    assert textos == ["tercera", "segunda", "primera"]  # desc por created_at
    assert all(n.author is not None and n.author.id == uid for n in notes)
    assert all(n.author.role == "admin" for n in notes)


@pytestmark_db
@pytest.mark.asyncio
async def test_delete_note_author_yes_other_no_admin_403():
    """Borrar: el autor puede; otro usuario no-admin → 403; un admin puede."""
    from fastapi import HTTPException

    from app.api.contacts import (
        ContactNoteCreate,
        create_contact_note,
        delete_contact_note,
        list_contact_notes,
    )
    from app.db.session import db_session
    from app.models.user import UserRole

    cid = await _seed_contact()
    author = await _get_user(await _seed_user(UserRole.cliente))
    other = await _get_user(await _seed_user(UserRole.cliente))
    admin = await _get_user(await _seed_user(UserRole.admin))

    # Nota del autor.
    async with db_session() as db:
        n1 = await create_contact_note(cid, ContactNoteCreate(texto="del autor"), db=db, current_user=author)

    # Otro cliente (no autor, no admin) → 403.
    with pytest.raises(HTTPException) as ei:
        async with db_session() as db:
            await delete_contact_note(cid, n1.id, db=db, current_user=other)
    assert ei.value.status_code == 403

    # El autor sí puede.
    async with db_session() as db:
        await delete_contact_note(cid, n1.id, db=db, current_user=author)
    async with db_session() as db:
        remaining = await list_contact_notes(cid, db=db, _=author)
    assert all(n.id != n1.id for n in remaining)

    # Una segunda nota del autor la puede borrar un admin.
    async with db_session() as db:
        n2 = await create_contact_note(cid, ContactNoteCreate(texto="otra"), db=db, current_user=author)
    async with db_session() as db:
        await delete_contact_note(cid, n2.id, db=db, current_user=admin)
    async with db_session() as db:
        remaining = await list_contact_notes(cid, db=db, _=admin)
    assert all(n.id != n2.id for n in remaining)


@pytestmark_db
@pytest.mark.asyncio
async def test_delete_note_404_when_not_belonging_to_contact():
    """404 si la nota no pertenece al contacto del path."""
    from fastapi import HTTPException

    from app.api.contacts import ContactNoteCreate, create_contact_note, delete_contact_note
    from app.db.session import db_session
    from app.models.user import UserRole

    cid_a = await _seed_contact()
    cid_b = await _seed_contact()
    admin = await _get_user(await _seed_user(UserRole.admin))

    async with db_session() as db:
        note = await create_contact_note(cid_a, ContactNoteCreate(texto="en A"), db=db, current_user=admin)

    # Misma nota, pero pedimos borrarla bajo el contacto B → 404.
    with pytest.raises(HTTPException) as ei:
        async with db_session() as db:
            await delete_contact_note(cid_b, note.id, db=db, current_user=admin)
    assert ei.value.status_code == 404


@pytestmark_db
@pytest.mark.asyncio
async def test_notes_cascade_on_contact_delete():
    """Al borrar el contacto, sus notas se borran (ON DELETE CASCADE)."""
    from sqlalchemy import func, select

    from app.api.contacts import ContactNoteCreate, create_contact_note
    from app.db.session import db_session
    from app.models.contact import Contact
    from app.models.contact_note import ContactNote
    from app.models.user import UserRole

    cid = await _seed_contact()
    admin = await _get_user(await _seed_user(UserRole.admin))

    async with db_session() as db:
        await create_contact_note(cid, ContactNoteCreate(texto="a"), db=db, current_user=admin)
        await create_contact_note(cid, ContactNoteCreate(texto="b"), db=db, current_user=admin)

    async with db_session() as db:
        count_before = (
            await db.execute(
                select(func.count()).select_from(ContactNote).where(ContactNote.contact_id == cid)
            )
        ).scalar_one()
    assert count_before == 2

    # Borramos el contacto (delete() del ORM dispara cascade ORM + DB).
    async with db_session() as db:
        contact = (await db.execute(select(Contact).where(Contact.id == cid))).scalar_one()
        await db.delete(contact)
        await db.commit()

    async with db_session() as db:
        count_after = (
            await db.execute(
                select(func.count()).select_from(ContactNote).where(ContactNote.contact_id == cid)
            )
        ).scalar_one()
    assert count_after == 0
