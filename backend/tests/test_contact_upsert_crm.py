"""Tests del tool crear_actualizar_contacto — promoción al CRM (in_crm).

Cuando el agente captura nombre o email de un contacto que estaba FUERA del
CRM (voz / Instagram, creados con in_crm=False), debe promoverlo a in_crm=True
para que aparezca en la pestaña Contactos. Sin identidad capturada (p.ej. solo
cambia el estado), el contacto se queda como estaba.

DB-gated con skipif (mismo patrón que test_voice_agent.py): los casos que tocan
BD se saltan si no hay Postgres disponible (se ejecutan en CI con Postgres).
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


pytestmark_db = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


@pytestmark_db
def test_captura_de_lead_promueve_a_crm():
    """Voz/IG: contacto in_crm=False → al capturar email pasa a in_crm=True."""
    from sqlalchemy import select

    from app.agents.tools.contact_upsert import crear_actualizar_contacto
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen

    telefono = f"voice:test-{uuid.uuid4().hex[:8]}"

    async def _run():
        async with db_session() as db:
            db.add(Contact(telefono=telefono, origen=ContactOrigen.manual, in_crm=False))
            await db.commit()
        cid = None
        try:
            out = await crear_actualizar_contacto(
                {"nombre": "Jessica", "email": "Jessica@example.com"},
                {"telefono": telefono},
            )
            assert '"updated"' in out

            async with db_session() as db:
                c = (
                    await db.execute(select(Contact).where(Contact.telefono == telefono))
                ).scalar_one()
                cid = c.id
                assert c.in_crm is True
                assert c.nombre == "Jessica"
                assert c.email == "jessica@example.com"  # normalizado a minúsculas
        finally:
            async with db_session() as db:
                c = (
                    await db.execute(select(Contact).where(Contact.telefono == telefono))
                ).scalar_one_or_none()
                if c:
                    await db.delete(c)
                    await db.commit()

    asyncio.run(_run())


@pytestmark_db
def test_sin_identidad_no_promueve():
    """Solo cambia el estado (sin nombre/email): un contacto fuera del CRM
    sigue fuera — no inflamos Contactos con visitantes anónimos."""
    from sqlalchemy import select

    from app.agents.tools.contact_upsert import crear_actualizar_contacto
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen

    telefono = f"voice:test-{uuid.uuid4().hex[:8]}"

    async def _run():
        async with db_session() as db:
            db.add(Contact(telefono=telefono, origen=ContactOrigen.manual, in_crm=False))
            await db.commit()
        try:
            out = await crear_actualizar_contacto(
                {"estado": "seguimiento"},
                {"telefono": telefono},
            )
            assert '"updated"' in out

            async with db_session() as db:
                c = (
                    await db.execute(select(Contact).where(Contact.telefono == telefono))
                ).scalar_one()
                assert c.in_crm is False
        finally:
            async with db_session() as db:
                c = (
                    await db.execute(select(Contact).where(Contact.telefono == telefono))
                ).scalar_one_or_none()
                if c:
                    await db.delete(c)
                    await db.commit()

    asyncio.run(_run())
