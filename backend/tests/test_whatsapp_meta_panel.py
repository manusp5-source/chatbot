"""Conectar WhatsApp con Meta desde el panel, por HTTP.

El alta de WhatsApp solo sabía de YCloud: las claves que pedía, la URL de
webhook que devolvía y el proveedor que acababa usando el envío eran de YCloud
y punto. Aquí se comprueba que el MISMO endpoint sirve para los dos, que la
elección se guarda en el canal (que es lo que lee el envío) y que el webhook de
Meta hace su saludo inicial y rechaza lo que no venga firmado.

Por HTTP y no llamando a la función: los permisos viven en las dependencias y
no se ejecutan si invocas el endpoint a mano.

DB-gated: se salta si no hay Postgres.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
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

_META_KEYS = (
    "meta_wa_phone_number_id",
    "meta_wa_business_account_id",
    "meta_wa_access_token",
    "meta_wa_app_secret",
    "meta_wa_verify_token",
)
_YCLOUD_KEYS = ("ycloud_api_key", "ycloud_webhook_secret", "ycloud_phone_number")

_ALTA_META = {
    "provider": "meta",
    "phone_number_id": "111111111111111",
    "business_account_id": "222222222222222",
    "access_token": "EAAG-token-de-prueba-largo",
    "app_secret": "secreto-de-la-app-abcdef",
    "verify_token": "palabra-de-verificacion-123",
    "phone_number": "+34600111222",
}


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _client():
    import httpx

    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _crear_admin() -> tuple[uuid.UUID, str]:
    from app.core.security import create_access_token, hash_password
    from app.db.session import db_session
    from app.models.user import User

    async with db_session() as db:
        u = User(
            email=f"wa-meta-{uuid.uuid4().hex[:8]}@test.local",
            password_hash=hash_password("UnaClaveLargaDeVerdad2026"),
            nombre="Admin WhatsApp",
            role="admin",
        )
        db.add(u)
        await db.commit()
        uid = u.id
    return uid, create_access_token(subject=str(uid), extra={"role": "admin"})


class _Aislamiento:
    """Aparta el canal de WhatsApp y sus credenciales, y los devuelve al final.

    El canal de WhatsApp es único por tipo y las credenciales son GLOBALES: si
    este test las deja puestas, otros verían un WhatsApp «configurado» que no lo
    está (el preflight de difusiones, por ejemplo).
    """

    def __init__(self) -> None:
        self.canales: list[uuid.UUID] = []
        self.creds: dict[str, bytes] = {}
        self.user_id: uuid.UUID | None = None
        self.creados: list[uuid.UUID] = []

    async def __aenter__(self):
        from sqlalchemy import select, text

        from app.db.session import db_session
        from app.models.channel import Channel, ChannelType
        from app.models.credential import Credential

        claves = (*_META_KEYS, *_YCLOUD_KEYS)
        async with db_session() as db:
            self.canales = list(
                (
                    await db.execute(
                        select(Channel.id).where(Channel.type == ChannelType.whatsapp)
                    )
                ).scalars().all()
            )
            for cid in self.canales:
                await db.execute(
                    text("UPDATE channels SET type = 'webchat' WHERE id = :i"), {"i": str(cid)}
                )
            self.creds = {
                c.key: c.value_encrypted
                for c in (
                    await db.execute(select(Credential).where(Credential.key.in_(claves)))
                ).scalars().all()
            }
            await db.execute(
                text("DELETE FROM credentials WHERE key = ANY(:k)"), {"k": list(claves)}
            )
            await db.commit()
        return self

    async def __aexit__(self, *_exc):
        from sqlalchemy import text

        from app.db.session import db_session
        from app.models.credential import Credential
        from app.services.credentials import invalidate_credential_cache

        claves = (*_META_KEYS, *_YCLOUD_KEYS)
        async with db_session() as db:
            for cid in self.creados:
                await db.execute(text("DELETE FROM channels WHERE id = :i"), {"i": str(cid)})
            await db.execute(
                text("DELETE FROM credentials WHERE key = ANY(:k)"), {"k": list(claves)}
            )
            for key, blob in self.creds.items():
                db.add(Credential(key=key, value_encrypted=blob))
            for cid in self.canales:
                await db.execute(
                    text("UPDATE channels SET type = 'whatsapp' WHERE id = :i"), {"i": str(cid)}
                )
            if self.user_id:
                await db.execute(
                    text("DELETE FROM audit_log WHERE user_id = :i"), {"i": str(self.user_id)}
                )
                await db.execute(
                    text("DELETE FROM users WHERE id = :i"), {"i": str(self.user_id)}
                )
            await db.commit()
        for key in claves:
            await invalidate_credential_cache(key)
        from app.providers.whatsapp import invalidate_provider_cache

        await invalidate_provider_cache()


# ---------------------------------------------------------------------------


def test_alta_con_meta_guarda_proveedor_claves_y_su_webhook():
    """Lo que de verdad importa: que el canal quede marcado como «meta» —que es
    lo que lee el envío—, que las cinco claves acaben cifradas y que la URL que
    se devuelve sea la de Meta, no la de YCloud."""
    from sqlalchemy import select

    from app.core.encryption import get_encryption_service
    from app.db.session import db_session
    from app.models.channel import Channel, ChannelType
    from app.models.credential import Credential

    async def _run():
        async with _Aislamiento() as iso:
            uid, token = await _crear_admin()
            iso.user_id = uid
            async with _client() as c:
                h = _headers(token)

                # Alta a medias: tiene que cantar y decir QUÉ falta, con el
                # nombre que se ve en el panel de Meta.
                r = await c.post(
                    "/api/v1/admin/channels/whatsapp/provision",
                    headers=h,
                    json={"provider": "meta", "phone_number_id": "solo-esto"},
                )
                assert r.status_code == 400, r.text
                detalle = r.json()["detail"]
                assert "token de acceso" in detalle, detalle
                assert "App secret" in detalle or "clave secreta" in detalle.lower(), detalle

                # Un proveedor que no existe tampoco cuela.
                r = await c.post(
                    "/api/v1/admin/channels/whatsapp/provision",
                    headers=h,
                    json={**_ALTA_META, "provider": "twilio"},
                )
                assert r.status_code == 400, r.text

                # Alta completa.
                r = await c.post(
                    "/api/v1/admin/channels/whatsapp/provision", headers=h, json=_ALTA_META
                )
                assert r.status_code == 200, r.text
                body = r.json()
                iso.creados.append(uuid.UUID(body["channel_id"]))
                assert body["provider"] == "meta"
                assert body["webhook_url"].endswith("/api/v1/webhooks/whatsapp/meta"), body
                assert body["verify_token"] == "palabra-de-verificacion-123"
                assert body["phone_number"] == "+34600111222"

            async with db_session() as db:
                ch = (
                    await db.execute(
                        select(Channel).where(Channel.type == ChannelType.whatsapp)
                    )
                ).scalar_one()
                assert ch.enabled is True
                assert ch.config["provider"] == "meta"
                # El canal apunta a SUS claves, no a las del otro proveedor.
                assert set(ch.config["legacy_credentials_keys"]) == set(_META_KEYS)

                enc = get_encryption_service()
                for key in _META_KEYS:
                    cred = (
                        await db.execute(select(Credential).where(Credential.key == key))
                    ).scalar_one_or_none()
                    assert cred is not None, f"no se guardó {key}"
                    assert enc.decrypt(cred.value_encrypted)
                secreto = (
                    await db.execute(
                        select(Credential).where(Credential.key == "meta_wa_app_secret")
                    )
                ).scalar_one()
                assert b"secreto-de-la-app" not in secreto.value_encrypted, (
                    "el secreto está en claro en la base de datos"
                )

    asyncio.run(_run())


def test_cambiar_de_ycloud_a_meta_y_volver():
    """El caso de quien prueba los dos proveedores. Al volver no se le puede
    pedir que reteclee lo que ya tenía guardado."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.channel import Channel, ChannelType

    async def _run():
        async with _Aislamiento() as iso:
            uid, token = await _crear_admin()
            iso.user_id = uid
            async with _client() as c:
                h = _headers(token)

                r = await c.post(
                    "/api/v1/admin/channels/whatsapp/provision",
                    headers=h,
                    json={
                        "provider": "ycloud",
                        "api_key": "yc-clave-de-prueba",
                        "webhook_secret": "secreto-de-firma",
                        "phone_number": "+34600111222",
                    },
                )
                assert r.status_code == 200, r.text
                channel_id = uuid.UUID(r.json()["channel_id"])
                iso.creados.append(channel_id)
                assert r.json()["webhook_url"].endswith("/api/v1/webhooks/ycloud")

                # A Meta: es un proveedor nuevo, así que se piden sus datos.
                r = await c.post(
                    "/api/v1/admin/channels/whatsapp/provision",
                    headers=h,
                    json={"provider": "meta"},
                )
                assert r.status_code == 400, r.text

                r = await c.post(
                    "/api/v1/admin/channels/whatsapp/provision", headers=h, json=_ALTA_META
                )
                assert r.status_code == 200, r.text
                # Upsert: sigue siendo EL MISMO canal, no uno nuevo.
                assert uuid.UUID(r.json()["channel_id"]) == channel_id

                # Y de vuelta a YCloud sin volver a escribir nada: lo suyo
                # sigue guardado.
                r = await c.post(
                    "/api/v1/admin/channels/whatsapp/provision",
                    headers=h,
                    json={"provider": "ycloud"},
                )
                assert r.status_code == 200, r.text
                assert r.json()["webhook_url"].endswith("/api/v1/webhooks/ycloud")

                r = await c.get("/api/v1/admin/channels", headers=h)
                wa = [ch for ch in r.json() if ch["type"] == "whatsapp"]
                assert len(wa) == 1, f"el canal se ha duplicado: {wa}"

            async with db_session() as db:
                ch = (
                    await db.execute(
                        select(Channel).where(Channel.type == ChannelType.whatsapp)
                    )
                ).scalar_one()
                assert ch.config["provider"] == "ycloud"

    asyncio.run(_run())


def test_el_envio_sale_por_el_proveedor_que_diga_el_canal():
    """La prueba de que la elección del panel llega hasta el envío: es lo que
    convierte «un campo en la base de datos» en «funciona con los dos»."""

    async def _run():
        async with _Aislamiento() as iso:
            uid, token = await _crear_admin()
            iso.user_id = uid
            async with _client() as c:
                r = await c.post(
                    "/api/v1/admin/channels/whatsapp/provision",
                    headers=_headers(token),
                    json=_ALTA_META,
                )
                assert r.status_code == 200, r.text
                iso.creados.append(uuid.UUID(r.json()["channel_id"]))

            from app.providers.whatsapp import get_provider_name, resolve_whatsapp_provider
            from app.providers.whatsapp.meta import MetaCloudProvider

            assert await get_provider_name() == "meta"
            assert isinstance(await resolve_whatsapp_provider(), MetaCloudProvider)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# El webhook de Meta
# ---------------------------------------------------------------------------


def test_el_webhook_de_meta_saluda_y_comprueba_la_firma():
    """Meta no deja guardar la URL si el saludo inicial no le devuelve su reto,
    y después firma cada aviso: si no comprobáramos la firma, cualquiera que
    supiera la URL podría colar mensajes falsos en la bandeja."""

    async def _run():
        async with _Aislamiento() as iso:
            uid, token = await _crear_admin()
            iso.user_id = uid
            async with _client() as c:
                r = await c.post(
                    "/api/v1/admin/channels/whatsapp/provision",
                    headers=_headers(token),
                    json=_ALTA_META,
                )
                assert r.status_code == 200, r.text
                iso.creados.append(uuid.UUID(r.json()["channel_id"]))

                # Saludo inicial con la palabra correcta.
                r = await c.get(
                    "/api/v1/webhooks/whatsapp/meta",
                    params={
                        "hub.mode": "subscribe",
                        "hub.verify_token": "palabra-de-verificacion-123",
                        "hub.challenge": "reto-1234",
                    },
                )
                assert r.status_code == 200, r.text
                assert r.text == "reto-1234"

                # Con otra palabra, no.
                r = await c.get(
                    "/api/v1/webhooks/whatsapp/meta",
                    params={
                        "hub.mode": "subscribe",
                        "hub.verify_token": "me-la-invento",
                        "hub.challenge": "reto-1234",
                    },
                )
                assert r.status_code == 403, r.text

                cuerpo = json.dumps(
                    {"object": "whatsapp_business_account", "entry": []}
                ).encode()

                # Sin firma: fuera.
                r = await c.post(
                    "/api/v1/webhooks/whatsapp/meta",
                    content=cuerpo,
                    headers={"content-type": "application/json"},
                )
                assert r.status_code == 401, r.text

                # Firmado con otro secreto: fuera.
                mala = hmac.new(b"otro-secreto", cuerpo, hashlib.sha256).hexdigest()
                r = await c.post(
                    "/api/v1/webhooks/whatsapp/meta",
                    content=cuerpo,
                    headers={
                        "content-type": "application/json",
                        "x-hub-signature-256": f"sha256={mala}",
                    },
                )
                assert r.status_code == 401, r.text

                # Bien firmado: entra (y no trae mensajes, que también vale).
                buena = hmac.new(
                    b"secreto-de-la-app-abcdef", cuerpo, hashlib.sha256
                ).hexdigest()
                r = await c.post(
                    "/api/v1/webhooks/whatsapp/meta",
                    content=cuerpo,
                    headers={
                        "content-type": "application/json",
                        "x-hub-signature-256": f"sha256={buena}",
                    },
                )
                assert r.status_code == 200, r.text
                assert r.json()["ok"] is True

    asyncio.run(_run())
