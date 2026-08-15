"""La lista de usuarios no puede caerse entera por una fila con mal correo.

Apareció montando el entorno de capturas: la base tenía un usuario con dominio
`.local` (reservado, y Pydantic lo rechaza) y `GET /admin/users` devolvía 500.
No fallaba ese usuario: fallaba la PANTALLA, así que desde el panel no había
manera ni de verlo ni de borrarlo.

Puede pasarle a cualquiera: `INITIAL_ADMIN_EMAIL` se guarda tal cual en el
primer arranque, sin pasar por la validación del panel.
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


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


@pytest.mark.asyncio
async def test_la_pantalla_de_usuarios_aguanta_un_correo_que_pydantic_rechaza():
    import httpx
    from sqlalchemy import text

    from app.core.security import create_access_token, hash_password
    from app.db.session import db_session
    from app.main import app
    from app.models.user import User

    suf = uuid.uuid4().hex[:8]
    async with db_session() as db:
        admin = User(
            email=f"admin-{suf}@ejemplo.com",
            password_hash=hash_password("UnaClaveLargaDeVerdad2026"),
            nombre="Admin Test",
            role="admin",
        )
        # El que rompía la pantalla: dominio reservado, correo válido para
        # Postgres y para el login, inválido para EmailStr.
        raro = User(
            email=f"pruebas-{suf}@test.local",
            password_hash=hash_password("OtraClaveLargaDeVerdad2026"),
            nombre="Usuario de pruebas",
            role="cliente",
        )
        db.add_all([admin, raro])
        await db.commit()
        ids = [admin.id, raro.id]
        token = create_access_token(subject=str(admin.id), extra={"role": "admin"})

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        correos = [u["email"] for u in r.json()]
        assert f"pruebas-{suf}@test.local" in correos, "el usuario raro tiene que SALIR, no desaparecer"
    finally:
        async with db_session() as db:
            for uid in ids:
                await db.execute(text("DELETE FROM audit_log WHERE user_id = :i"), {"i": str(uid)})
                await db.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(uid)})
            await db.commit()
