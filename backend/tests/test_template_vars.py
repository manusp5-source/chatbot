"""Tests de los metadatos AMIGABLES de variables de plantilla de WhatsApp.

Capa puramente aditiva encima del envío (NO toca ycloud.send_template ni
/outbound/send). Cobertura (DB-gated con skipif, mismo patrón que
test_contact_activity.py):

  - PUT + GET roundtrip: lo que guardas es lo que lees, ordenado por idx.
  - GET vacío si la plantilla no se ha configurado.
  - PUT reemplaza (no acumula): un segundo PUT deja solo lo nuevo.
  - Validación rechaza un contact_field fuera del set permitido (HTTP 400).
  - resolve: rellena los idx enlazados desde un contacto, deja fuera los no
    enlazados (relleno manual), y marca contact_found=false con teléfono
    desconocido.

Los endpoints se llaman directamente como funciones (inyectando un AsyncSession
real y el usuario), evitando levantar el stack HTTP/auth completo.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


# ---------------------------------------------------------------------------
# DB-gate (mismo patrón que test_contact_activity.py)
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


async def _seed_user():
    from app.db.session import db_session
    from app.models.user import User, UserRole

    async with db_session() as db:
        user = User(
            email=f"user-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="x",
            role=UserRole.admin,
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


async def _seed_contact(telefono: str, **fields):
    """Crea un contacto con campos arbitrarios y devuelve su teléfono normalizado."""
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.providers.whatsapp.ycloud import _normalize_phone

    normalized = _normalize_phone(telefono)
    async with db_session() as db:
        c = Contact(
            telefono=normalized,
            origen=fields.pop("origen", ContactOrigen.whatsapp),
            **fields,
        )
        db.add(c)
        await db.commit()
    return normalized


def _unique_template_name() -> str:
    return f"tpl_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_put_get_roundtrip_ordered_by_idx():
    """PUT guarda y GET devuelve lo mismo, ordenado por idx."""
    from app.api.admin import (
        TemplateVarIn,
        TemplateVarsPut,
        get_template_vars,
        put_template_vars,
    )
    from app.db.session import db_session

    user = await _get_user(await _seed_user())
    name = _unique_template_name()

    async with db_session() as db:
        saved = await put_template_vars(
            name,
            TemplateVarsPut(
                language="es",
                # A propósito desordenado para comprobar el ORDER BY idx.
                vars=[
                    TemplateVarIn(idx=2, nombre="Servicio", contact_field="servicio_interes"),
                    TemplateVarIn(idx=1, nombre="Nombre del contacto", contact_field="nombre"),
                ],
            ),
            db=db,
            user=user,
        )

    assert [v.idx for v in saved] == [1, 2]
    assert saved[0].nombre == "Nombre del contacto"
    assert saved[0].contact_field == "nombre"
    assert saved[1].contact_field == "servicio_interes"

    async with db_session() as db:
        fetched = await get_template_vars(name, language="es", db=db, _=user)

    assert [(v.idx, v.nombre, v.contact_field) for v in fetched] == [
        (1, "Nombre del contacto", "nombre"),
        (2, "Servicio", "servicio_interes"),
    ]


@pytestmark_db
@pytest.mark.asyncio
async def test_get_empty_when_not_configured():
    """GET devuelve lista vacía si la plantilla/idioma no se ha configurado."""
    from app.api.admin import get_template_vars
    from app.db.session import db_session

    user = await _get_user(await _seed_user())

    async with db_session() as db:
        fetched = await get_template_vars(
            _unique_template_name(), language="es", db=db, _=user
        )
    assert fetched == []


@pytestmark_db
@pytest.mark.asyncio
async def test_put_replaces_does_not_accumulate():
    """Un segundo PUT REEMPLAZA la config (no acumula filas)."""
    from app.api.admin import (
        TemplateVarIn,
        TemplateVarsPut,
        get_template_vars,
        put_template_vars,
    )
    from app.db.session import db_session

    user = await _get_user(await _seed_user())
    name = _unique_template_name()

    async with db_session() as db:
        await put_template_vars(
            name,
            TemplateVarsPut(
                language="es",
                vars=[
                    TemplateVarIn(idx=1, nombre="A", contact_field="nombre"),
                    TemplateVarIn(idx=2, nombre="B", contact_field="email"),
                    TemplateVarIn(idx=3, nombre="C"),
                ],
            ),
            db=db,
            user=user,
        )

    # Segundo PUT con menos variables y distintos valores.
    async with db_session() as db:
        await put_template_vars(
            name,
            TemplateVarsPut(
                language="es",
                vars=[TemplateVarIn(idx=1, nombre="Solo una", contact_field="telefono")],
            ),
            db=db,
            user=user,
        )

    async with db_session() as db:
        fetched = await get_template_vars(name, language="es", db=db, _=user)

    assert len(fetched) == 1
    assert fetched[0].idx == 1
    assert fetched[0].nombre == "Solo una"
    assert fetched[0].contact_field == "telefono"


@pytestmark_db
@pytest.mark.asyncio
async def test_put_isolates_per_language():
    """Configurar un idioma no borra la config de otro idioma de la misma plantilla."""
    from app.api.admin import (
        TemplateVarIn,
        TemplateVarsPut,
        get_template_vars,
        put_template_vars,
    )
    from app.db.session import db_session

    user = await _get_user(await _seed_user())
    name = _unique_template_name()

    async with db_session() as db:
        await put_template_vars(
            name,
            TemplateVarsPut(language="es", vars=[TemplateVarIn(idx=1, nombre="ES")]),
            db=db,
            user=user,
        )
    async with db_session() as db:
        await put_template_vars(
            name,
            TemplateVarsPut(language="en", vars=[TemplateVarIn(idx=1, nombre="EN")]),
            db=db,
            user=user,
        )

    async with db_session() as db:
        es = await get_template_vars(name, language="es", db=db, _=user)
        en = await get_template_vars(name, language="en", db=db, _=user)

    assert len(es) == 1 and es[0].nombre == "ES"
    assert len(en) == 1 and en[0].nombre == "EN"


@pytestmark_db
@pytest.mark.asyncio
async def test_validation_rejects_invalid_contact_field():
    """PUT rechaza un contact_field fuera del set permitido (HTTP 400)."""
    from fastapi import HTTPException

    from app.api.admin import TemplateVarIn, TemplateVarsPut, put_template_vars
    from app.db.session import db_session

    user = await _get_user(await _seed_user())
    name = _unique_template_name()

    with pytest.raises(HTTPException) as exc:
        async with db_session() as db:
            await put_template_vars(
                name,
                TemplateVarsPut(
                    language="es",
                    # 'notas_internas' NO está en el set permitido (es PII cifrada).
                    vars=[TemplateVarIn(idx=1, nombre="X", contact_field="notas_internas")],
                ),
                db=db,
                user=user,
            )
    assert exc.value.status_code == 400


@pytestmark_db
@pytest.mark.asyncio
async def test_validation_rejects_idx_below_one():
    """PUT rechaza idx < 1 (HTTP 400)."""
    from fastapi import HTTPException

    from app.api.admin import TemplateVarIn, TemplateVarsPut, put_template_vars
    from app.db.session import db_session

    user = await _get_user(await _seed_user())

    with pytest.raises(HTTPException) as exc:
        async with db_session() as db:
            await put_template_vars(
                _unique_template_name(),
                TemplateVarsPut(language="es", vars=[TemplateVarIn(idx=0, nombre="X")]),
                db=db,
                user=user,
            )
    assert exc.value.status_code == 400


@pytestmark_db
@pytest.mark.asyncio
async def test_resolve_fills_linked_leaves_unlinked_out():
    """resolve rellena los idx enlazados desde el contacto y deja fuera los no enlazados."""
    from app.api.admin import (
        ResolveBody,
        TemplateVarIn,
        TemplateVarsPut,
        put_template_vars,
        resolve_template_vars,
    )
    from app.db.session import db_session

    user = await _get_user(await _seed_user())
    name = _unique_template_name()
    phone = await _seed_contact(
        f"+34{uuid.uuid4().int % 10**9:09d}",
        nombre="Ana",
        servicio_interes="Reforma cocina",
    )

    async with db_session() as db:
        await put_template_vars(
            name,
            TemplateVarsPut(
                language="es",
                vars=[
                    TemplateVarIn(idx=1, nombre="Nombre", contact_field="nombre"),
                    TemplateVarIn(idx=2, nombre="Servicio", contact_field="servicio_interes"),
                    # idx=3 sin enlace → manual → no debe aparecer en la respuesta.
                    TemplateVarIn(idx=3, nombre="Fecha", contact_field=None),
                ],
            ),
            db=db,
            user=user,
        )

    async with db_session() as db:
        out = await resolve_template_vars(
            name, ResolveBody(language="es", phones=[phone]), db=db, _=user
        )

    assert len(out) == 1
    assert out[0].phone == phone
    assert out[0].contact_found is True
    # Solo los enlazados con valor; idx 3 (manual) queda fuera.
    assert out[0].variables == {"1": "Ana", "2": "Reforma cocina"}


@pytestmark_db
@pytest.mark.asyncio
async def test_resolve_skips_linked_field_with_no_value():
    """Un campo enlazado pero vacío en el contacto no se incluye (relleno manual)."""
    from app.api.admin import (
        ResolveBody,
        TemplateVarIn,
        TemplateVarsPut,
        put_template_vars,
        resolve_template_vars,
    )
    from app.db.session import db_session

    user = await _get_user(await _seed_user())
    name = _unique_template_name()
    # Contacto SIN servicio_interes (None).
    phone = await _seed_contact(f"+34{uuid.uuid4().int % 10**9:09d}", nombre="Beto")

    async with db_session() as db:
        await put_template_vars(
            name,
            TemplateVarsPut(
                language="es",
                vars=[
                    TemplateVarIn(idx=1, nombre="Nombre", contact_field="nombre"),
                    TemplateVarIn(idx=2, nombre="Servicio", contact_field="servicio_interes"),
                ],
            ),
            db=db,
            user=user,
        )

    async with db_session() as db:
        out = await resolve_template_vars(
            name, ResolveBody(language="es", phones=[phone]), db=db, _=user
        )

    assert out[0].contact_found is True
    # idx 2 (servicio_interes None) queda fuera; solo el nombre.
    assert out[0].variables == {"1": "Beto"}


@pytestmark_db
@pytest.mark.asyncio
async def test_resolve_unknown_phone_marks_not_found():
    """resolve con un teléfono desconocido marca contact_found=false y variables vacío."""
    from app.api.admin import (
        ResolveBody,
        TemplateVarIn,
        TemplateVarsPut,
        put_template_vars,
        resolve_template_vars,
    )
    from app.db.session import db_session

    user = await _get_user(await _seed_user())
    name = _unique_template_name()

    async with db_session() as db:
        await put_template_vars(
            name,
            TemplateVarsPut(
                language="es",
                vars=[TemplateVarIn(idx=1, nombre="Nombre", contact_field="nombre")],
            ),
            db=db,
            user=user,
        )

    unknown = f"+34{uuid.uuid4().int % 10**9:09d}"
    async with db_session() as db:
        out = await resolve_template_vars(
            name, ResolveBody(language="es", phones=[unknown]), db=db, _=user
        )

    assert len(out) == 1
    assert out[0].contact_found is False
    assert out[0].variables == {}
