"""El botón "Probar" de las claves dice algo útil.

Antes, con la clave de YCloud o la de Instagram, contestaba "Guardado (test no
implementado para esta clave)", que se lee como un fallo del programa y deja a
quien configura sin saber si su clave vale. Ahora YCloud se comprueba de verdad
contra su API, y las que no se pueden comprobar lo dicen con esas palabras.
"""
from __future__ import annotations

import asyncio

import httpx
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


async def _admin_token() -> tuple[str, list]:
    import uuid

    from app.core.security import create_access_token, hash_password
    from app.db.session import db_session
    from app.models.user import User

    suf = uuid.uuid4().hex[:8]
    async with db_session() as db:
        admin = User(
            email=f"admin-{suf}@ejemplo.com",
            password_hash=hash_password("UnaClaveLargaDeVerdad2026"),
            nombre="Admin Test",
            role="admin",
        )
        db.add(admin)
        await db.commit()
        return create_access_token(subject=str(admin.id), extra={"role": "admin"}), [admin.id]


async def _guardar_credencial(key: str, valor: str) -> None:
    from sqlalchemy import select

    from app.core.encryption import get_encryption_service
    from app.db.session import db_session
    from app.models.credential import Credential

    async with db_session() as db:
        fila = (
            await db.execute(select(Credential).where(Credential.key == key))
        ).scalar_one_or_none()
        cifrado = get_encryption_service().encrypt(valor)
        if fila:
            fila.value_encrypted = cifrado
        else:
            db.add(Credential(key=key, value_encrypted=cifrado))
        await db.commit()


async def _limpiar(key: str, ids: list) -> None:
    from sqlalchemy import text

    from app.db.session import db_session

    async with db_session() as db:
        await db.execute(text("DELETE FROM credentials WHERE key = :k"), {"k": key})
        for uid in ids:
            await db.execute(text("DELETE FROM audit_log WHERE user_id = :i"), {"i": str(uid)})
            await db.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(uid)})
        await db.commit()


@pytest.mark.asyncio
async def test_ycloud_con_numero_dado_de_alta(monkeypatch):
    from app.main import app

    token, ids = await _admin_token()
    await _guardar_credencial("ycloud_api_key", "clave-de-mentira")

    async def fake_get(self, url, **kw):  # noqa: ANN001
        assert "phoneNumbers" in url
        assert kw["headers"]["X-API-Key"] == "clave-de-mentira"
        return httpx.Response(200, json={"items": [{"phoneNumber": "+34600112233"}]})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/api/v1/admin/credentials/ycloud_api_key/test",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200, r.text
        cuerpo = r.json()
        assert cuerpo["ok"] is True
        assert "+34600112233" in cuerpo["message"]
    finally:
        await _limpiar("ycloud_api_key", ids)


@pytest.mark.asyncio
async def test_ycloud_con_clave_rechazada(monkeypatch):
    from app.main import app

    token, ids = await _admin_token()
    await _guardar_credencial("ycloud_api_key", "clave-mala")

    async def fake_get(self, url, **kw):  # noqa: ANN001
        return httpx.Response(401, json={"error": "unauthorized"})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/api/v1/admin/credentials/ycloud_api_key/test",
                headers={"Authorization": f"Bearer {token}"},
            )
        cuerpo = r.json()
        assert cuerpo["ok"] is False
        assert "rechaza" in cuerpo["message"].lower()
    finally:
        await _limpiar("ycloud_api_key", ids)


@pytest.mark.asyncio
async def test_la_que_no_se_puede_probar_lo_dice_sin_sonar_a_fallo():
    from app.main import app

    token, ids = await _admin_token()
    await _guardar_credencial("ycloud_webhook_secret", "un-secreto")

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/api/v1/admin/credentials/ycloud_webhook_secret/test",
                headers={"Authorization": f"Bearer {token}"},
            )
        cuerpo = r.json()
        assert cuerpo["ok"] is True
        assert "no implementado" not in cuerpo["message"]
        assert "primer mensaje" in cuerpo["message"]
    finally:
        await _limpiar("ycloud_webhook_secret", ids)
