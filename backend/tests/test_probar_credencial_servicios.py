"""El botón "Probar" de la pestaña Servicios comprueba de verdad.

Los proveedores LLM ya se probaban solos; las tarjetas de Servicios (Resend, el
correo saliente, Google, Instagram y las dos de WhatsApp) contestaban todas lo
mismo, así que quien configuraba se quedaba sin saber si su clave valía.

Cada comprobación se prueba en sus tres casos: la credencial vale, el proveedor
la rechaza, y no se pudo llegar al proveedor. La red va siempre simulada
(monkeypatch sobre el cliente HTTP y sobre smtplib): ningún test sale a
internet.
"""
from __future__ import annotations

import asyncio
import smtplib

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


# ---------------------------------------------------------------------------
# Andamiaje
# ---------------------------------------------------------------------------


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


async def _guardar(**valores: str) -> None:
    """Guarda credenciales cifradas Y limpia la caché.

    Lo segundo importa: `get_credential` cachea en Redis 60 s, así que sin
    invalidar, un test heredaría el valor del anterior.
    """
    from sqlalchemy import select

    from app.core.encryption import get_encryption_service
    from app.db.session import db_session
    from app.models.credential import Credential
    from app.services.credentials import invalidate_credential_cache

    async with db_session() as db:
        for key, valor in valores.items():
            fila = (
                await db.execute(select(Credential).where(Credential.key == key))
            ).scalar_one_or_none()
            cifrado = get_encryption_service().encrypt(valor)
            if fila:
                fila.value_encrypted = cifrado
            else:
                db.add(Credential(key=key, value_encrypted=cifrado))
        await db.commit()
    for key in valores:
        await invalidate_credential_cache(key)


async def _limpiar(keys: list[str], ids: list) -> None:
    from sqlalchemy import text

    from app.db.session import db_session
    from app.services.credentials import invalidate_credential_cache

    async with db_session() as db:
        for k in keys:
            await db.execute(text("DELETE FROM credentials WHERE key = :k"), {"k": k})
        for uid in ids:
            await db.execute(text("DELETE FROM audit_log WHERE user_id = :i"), {"i": str(uid)})
            await db.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(uid)})
        await db.commit()
    for k in keys:
        await invalidate_credential_cache(k)


async def _probar(token: str, key: str) -> dict:
    """Pulsa "Probar" sobre una clave y devuelve lo que contesta el panel."""
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(
            f"/api/v1/admin/credentials/{key}/test",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    return r.json()


def _falsear_get(monkeypatch, responder) -> None:
    """Sustituye el GET de httpx. `responder(url, kw)` devuelve la respuesta o
    lanza para simular un fallo de red."""

    async def fake_get(self, url, **kw):  # noqa: ANN001
        return responder(str(url), kw)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


def _falsear_post(monkeypatch, host: str, responder) -> None:
    """Sustituye el POST de httpx SOLO para `host`.

    El resto pasa al original a propósito: el propio cliente de test llama al
    backend con POST, y si lo interceptáramos no quedaría test que valiera.
    """
    original = httpx.AsyncClient.post

    async def fake_post(self, url, **kw):  # noqa: ANN001
        if host not in str(url):
            return await original(self, url, **kw)
        return responder(str(url), kw)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


def _corta_la_red(*_a, **_kw):
    raise httpx.ConnectError("sin salida a internet")


# ---------------------------------------------------------------------------
# Resend — la clave de API
# ---------------------------------------------------------------------------

RESEND_VERIFICADO = {"data": [{"name": "chatbot.com", "status": "verified"}]}


@pytest.mark.asyncio
async def test_resend_clave_buena_con_dominio_verificado(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(resend_api_key="re_de_mentira")

    def responder(url, kw):
        assert "api.resend.com/domains" in url
        assert kw["headers"]["Authorization"] == "Bearer re_de_mentira"
        return httpx.Response(200, json=RESEND_VERIFICADO)

    _falsear_get(monkeypatch, responder)
    try:
        cuerpo = await _probar(token, "resend_api_key")
        assert cuerpo["ok"] is True
        assert "chatbot.com" in cuerpo["message"]
    finally:
        await _limpiar(["resend_api_key"], ids)


@pytest.mark.asyncio
async def test_resend_clave_rechazada(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(resend_api_key="re_mala")

    _falsear_get(monkeypatch, lambda url, kw: httpx.Response(401, json={"message": "invalid"}))
    try:
        cuerpo = await _probar(token, "resend_api_key")
        assert cuerpo["ok"] is False
        assert "rechaza" in cuerpo["message"].lower()
        assert "re_mala" not in cuerpo["message"]
    finally:
        await _limpiar(["resend_api_key"], ids)


@pytest.mark.asyncio
async def test_resend_clave_buena_pero_sin_dominio_verificado(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(resend_api_key="re_de_mentira")

    _falsear_get(
        monkeypatch,
        lambda url, kw: httpx.Response(
            200, json={"data": [{"name": "chatbot.com", "status": "pending"}]}
        ),
    )
    try:
        cuerpo = await _probar(token, "resend_api_key")
        assert cuerpo["ok"] is False
        assert "verificad" in cuerpo["message"].lower()
    finally:
        await _limpiar(["resend_api_key"], ids)


@pytest.mark.asyncio
async def test_resend_error_de_red(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(resend_api_key="re_de_mentira")

    _falsear_get(monkeypatch, _corta_la_red)
    try:
        cuerpo = await _probar(token, "resend_api_key")
        assert cuerpo["ok"] is False
        assert "red" in cuerpo["message"].lower()
    finally:
        await _limpiar(["resend_api_key"], ids)


# ---------------------------------------------------------------------------
# Resend — el remitente
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remitente_con_dominio_verificado(monkeypatch):
    token, ids = await _admin_token()
    # El remitente tiene que estar EN el dominio que RESEND_VERIFICADO da por
    # verificado (chatbot.com). Con otro dominio, el servicio responde ok=False
    # —y hace bien—, que es justo lo que cubre el test siguiente.
    await _guardar(resend_api_key="re_de_mentira", resend_from_email="Avisos <hola@chatbot.com>")

    _falsear_get(monkeypatch, lambda url, kw: httpx.Response(200, json=RESEND_VERIFICADO))
    try:
        cuerpo = await _probar(token, "resend_from_email")
        assert cuerpo["ok"] is True
        assert "chatbot.com" in cuerpo["message"]
    finally:
        await _limpiar(["resend_api_key", "resend_from_email"], ids)


@pytest.mark.asyncio
async def test_remitente_de_un_dominio_que_no_esta_en_resend(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(resend_api_key="re_de_mentira", resend_from_email="hola@otrodominio.com")

    _falsear_get(monkeypatch, lambda url, kw: httpx.Response(200, json=RESEND_VERIFICADO))
    try:
        cuerpo = await _probar(token, "resend_from_email")
        assert cuerpo["ok"] is False
        assert "otrodominio.com" in cuerpo["message"]
    finally:
        await _limpiar(["resend_api_key", "resend_from_email"], ids)


@pytest.mark.asyncio
async def test_remitente_error_de_red(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(resend_api_key="re_de_mentira", resend_from_email="hola@example.com")

    _falsear_get(monkeypatch, _corta_la_red)
    try:
        cuerpo = await _probar(token, "resend_from_email")
        assert cuerpo["ok"] is False
        assert "red" in cuerpo["message"].lower()
    finally:
        await _limpiar(["resend_api_key", "resend_from_email"], ids)


# ---------------------------------------------------------------------------
# Correo saliente (SMTP)
# ---------------------------------------------------------------------------

SMTP_KEYS = ["smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from"]


class _SMTPFalso:
    """Servidor de mentira. `fallo_login` para simular que rechaza la clave."""

    fallo_login = False

    def __init__(self, host, port, timeout=None):  # noqa: ANN001
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        return (250, b"ok")

    def starttls(self):
        return (220, b"ok")

    def login(self, user, pwd):  # noqa: ANN001
        if type(self).fallo_login:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
        return (235, b"ok")


@pytest.mark.asyncio
async def test_smtp_credenciales_buenas(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(
        smtp_host="smtp.gmail.com",
        smtp_port="587",
        smtp_user="hola@example.com",
        smtp_password="una-contrasena-de-aplicacion",
        smtp_from="hola@example.com",
    )

    _SMTPFalso.fallo_login = False
    monkeypatch.setattr(smtplib, "SMTP", _SMTPFalso)
    try:
        cuerpo = await _probar(token, "smtp_password")
        assert cuerpo["ok"] is True
        assert "smtp.gmail.com" in cuerpo["message"]
        # Ni la contraseña ni el usuario se devuelven al panel.
        assert "una-contrasena-de-aplicacion" not in cuerpo["message"]
    finally:
        await _limpiar(SMTP_KEYS, ids)


@pytest.mark.asyncio
async def test_smtp_contrasena_rechazada(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(
        smtp_host="smtp.gmail.com",
        smtp_port="587",
        smtp_user="hola@example.com",
        smtp_password="la-de-la-cuenta",
        smtp_from="hola@example.com",
    )

    _SMTPFalso.fallo_login = True
    monkeypatch.setattr(smtplib, "SMTP", _SMTPFalso)
    try:
        cuerpo = await _probar(token, "smtp_password")
        assert cuerpo["ok"] is False
        assert "contraseña de aplicación" in cuerpo["message"]
        assert "la-de-la-cuenta" not in cuerpo["message"]
    finally:
        _SMTPFalso.fallo_login = False
        await _limpiar(SMTP_KEYS, ids)


@pytest.mark.asyncio
async def test_smtp_servidor_inalcanzable(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(
        smtp_host="smtp.inventado.test",
        smtp_port="587",
        smtp_user="hola@example.com",
        smtp_password="una-contrasena-de-aplicacion",
        smtp_from="hola@example.com",
    )

    def _no_conecta(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(smtplib, "SMTP", _no_conecta)
    try:
        cuerpo = await _probar(token, "smtp_host")
        assert cuerpo["ok"] is False
        assert "No se pudo conectar" in cuerpo["message"]
        assert "smtp.inventado.test:587" in cuerpo["message"]
    finally:
        await _limpiar(SMTP_KEYS, ids)


@pytest.mark.asyncio
async def test_smtp_entra_pero_falta_el_remitente(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(
        smtp_host="smtp.gmail.com",
        smtp_port="587",
        smtp_user="hola@example.com",
        smtp_password="una-contrasena-de-aplicacion",
        smtp_from="",
    )

    _SMTPFalso.fallo_login = False
    monkeypatch.setattr(smtplib, "SMTP", _SMTPFalso)
    try:
        cuerpo = await _probar(token, "smtp_user")
        assert cuerpo["ok"] is False
        assert "remitente" in cuerpo["message"]
    finally:
        await _limpiar(SMTP_KEYS, ids)


# ---------------------------------------------------------------------------
# Google — el par identificador + secreto
# ---------------------------------------------------------------------------

GOOGLE_KEYS = ["google_oauth_client_id", "google_oauth_client_secret"]


@pytest.mark.asyncio
async def test_google_par_valido(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(
        google_oauth_client_id="123.apps.googleusercontent.com",
        google_oauth_client_secret="GOCSPX-secreto",
    )

    def responder(url, kw):
        # El secreto viaja en el CUERPO, nunca en la URL.
        assert "GOCSPX-secreto" not in url
        assert kw["data"]["client_secret"] == "GOCSPX-secreto"
        # Par bueno + permiso inventado = "invalid_grant".
        return httpx.Response(400, json={"error": "invalid_grant"})

    _falsear_post(monkeypatch, "oauth2.googleapis.com", responder)
    try:
        cuerpo = await _probar(token, "google_oauth_client_id")
        assert cuerpo["ok"] is True
        assert "Google OK" in cuerpo["message"]
    finally:
        await _limpiar(GOOGLE_KEYS, ids)


@pytest.mark.asyncio
async def test_google_par_rechazado(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(
        google_oauth_client_id="123.apps.googleusercontent.com",
        google_oauth_client_secret="GOCSPX-mal-copiado",
    )

    _falsear_post(
        monkeypatch,
        "oauth2.googleapis.com",
        lambda url, kw: httpx.Response(401, json={"error": "invalid_client"}),
    )
    try:
        cuerpo = await _probar(token, "google_oauth_client_secret")
        assert cuerpo["ok"] is False
        assert "rechaza" in cuerpo["message"].lower()
        assert "GOCSPX-mal-copiado" not in cuerpo["message"]
    finally:
        await _limpiar(GOOGLE_KEYS, ids)


@pytest.mark.asyncio
async def test_google_falta_la_otra_mitad_del_par(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(google_oauth_client_id="123.apps.googleusercontent.com")

    _falsear_post(monkeypatch, "oauth2.googleapis.com", _corta_la_red)
    try:
        cuerpo = await _probar(token, "google_oauth_client_id")
        assert cuerpo["ok"] is False
        assert "secreto de cliente" in cuerpo["message"]
    finally:
        await _limpiar(GOOGLE_KEYS, ids)


@pytest.mark.asyncio
async def test_google_error_de_red(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(
        google_oauth_client_id="123.apps.googleusercontent.com",
        google_oauth_client_secret="GOCSPX-secreto",
    )

    _falsear_post(monkeypatch, "oauth2.googleapis.com", _corta_la_red)
    try:
        cuerpo = await _probar(token, "google_oauth_client_id")
        assert cuerpo["ok"] is False
        assert "red" in cuerpo["message"].lower()
    finally:
        await _limpiar(GOOGLE_KEYS, ids)


# ---------------------------------------------------------------------------
# Instagram — el par de la app de Meta
# ---------------------------------------------------------------------------

IG_KEYS = ["instagram_oauth_client_id", "instagram_oauth_client_secret"]


@pytest.mark.asyncio
async def test_instagram_par_valido(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(
        instagram_oauth_client_id="1234567890",
        instagram_oauth_client_secret="secreto-de-la-app",
    )

    def responder(url, kw):
        assert "oauth/access_token" in url
        assert kw["params"]["grant_type"] == "client_credentials"
        return httpx.Response(200, json={"access_token": "1234567890|abc", "token_type": "bearer"})

    _falsear_get(monkeypatch, responder)
    try:
        cuerpo = await _probar(token, "instagram_oauth_client_secret")
        assert cuerpo["ok"] is True
        assert "Instagram OK" in cuerpo["message"]
    finally:
        await _limpiar(IG_KEYS, ids)


@pytest.mark.asyncio
async def test_instagram_par_rechazado(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(
        instagram_oauth_client_id="1234567890",
        instagram_oauth_client_secret="secreto-mal-copiado",
    )

    _falsear_get(
        monkeypatch,
        lambda url, kw: httpx.Response(400, json={"error": {"message": "Invalid client secret"}}),
    )
    try:
        cuerpo = await _probar(token, "instagram_oauth_client_secret")
        assert cuerpo["ok"] is False
        assert "rechaza" in cuerpo["message"].lower()
        # Ni el secreto ni el error crudo de Meta salen al panel.
        assert "secreto-mal-copiado" not in cuerpo["message"]
        assert "Invalid client secret" not in cuerpo["message"]
    finally:
        await _limpiar(IG_KEYS, ids)


@pytest.mark.asyncio
async def test_instagram_error_de_red(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(
        instagram_oauth_client_id="1234567890",
        instagram_oauth_client_secret="secreto-de-la-app",
    )

    _falsear_get(monkeypatch, _corta_la_red)
    try:
        cuerpo = await _probar(token, "instagram_oauth_client_id")
        assert cuerpo["ok"] is False
        assert "red" in cuerpo["message"].lower()
        assert "secreto-de-la-app" not in cuerpo["message"]
    finally:
        await _limpiar(IG_KEYS, ids)


# ---------------------------------------------------------------------------
# WhatsApp — el número de YCloud
# ---------------------------------------------------------------------------

YCLOUD_KEYS = ["ycloud_api_key", "ycloud_phone_number"]
YCLOUD_ALTA = {"items": [{"phoneNumber": "+34600112233"}]}


@pytest.mark.asyncio
async def test_numero_ycloud_dado_de_alta(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(ycloud_api_key="clave-de-mentira", ycloud_phone_number="+34 600 11 22 33")

    _falsear_get(monkeypatch, lambda url, kw: httpx.Response(200, json=YCLOUD_ALTA))
    try:
        cuerpo = await _probar(token, "ycloud_phone_number")
        assert cuerpo["ok"] is True
        assert "dado de alta" in cuerpo["message"]
    finally:
        await _limpiar(YCLOUD_KEYS, ids)


@pytest.mark.asyncio
async def test_numero_ycloud_que_no_esta_en_la_cuenta(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(ycloud_api_key="clave-de-mentira", ycloud_phone_number="+34699999999")

    _falsear_get(monkeypatch, lambda url, kw: httpx.Response(200, json=YCLOUD_ALTA))
    try:
        cuerpo = await _probar(token, "ycloud_phone_number")
        assert cuerpo["ok"] is False
        assert "no está dado de alta" in cuerpo["message"]
        assert "+34600112233" in cuerpo["message"]
    finally:
        await _limpiar(YCLOUD_KEYS, ids)


@pytest.mark.asyncio
async def test_numero_ycloud_error_de_red(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(ycloud_api_key="clave-de-mentira", ycloud_phone_number="+34600112233")

    _falsear_get(monkeypatch, _corta_la_red)
    try:
        cuerpo = await _probar(token, "ycloud_phone_number")
        assert cuerpo["ok"] is False
        assert "red" in cuerpo["message"].lower()
    finally:
        await _limpiar(YCLOUD_KEYS, ids)


# ---------------------------------------------------------------------------
# WhatsApp — los dos identificadores de Meta
# ---------------------------------------------------------------------------

META_KEYS = ["meta_wa_access_token", "meta_wa_phone_number_id", "meta_wa_business_account_id"]


@pytest.mark.asyncio
async def test_identificador_de_numero_meta_valido(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(meta_wa_access_token="EAA-de-mentira", meta_wa_phone_number_id="998877")

    def responder(url, kw):
        assert url.endswith("/998877")
        assert kw["headers"]["Authorization"] == "Bearer EAA-de-mentira"
        return httpx.Response(200, json={"display_phone_number": "+34 600 11 22 33"})

    _falsear_get(monkeypatch, responder)
    try:
        cuerpo = await _probar(token, "meta_wa_phone_number_id")
        assert cuerpo["ok"] is True
        assert "+34 600 11 22 33" in cuerpo["message"]
    finally:
        await _limpiar(META_KEYS, ids)


@pytest.mark.asyncio
async def test_identificador_de_numero_meta_rechazado(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(meta_wa_access_token="EAA-de-mentira", meta_wa_phone_number_id="600112233")

    _falsear_get(
        monkeypatch,
        lambda url, kw: httpx.Response(404, json={"error": {"message": "Unsupported get request"}}),
    )
    try:
        cuerpo = await _probar(token, "meta_wa_phone_number_id")
        assert cuerpo["ok"] is False
        assert "no reconoce" in cuerpo["message"]
        assert "Unsupported get request" not in cuerpo["message"]
    finally:
        await _limpiar(META_KEYS, ids)


@pytest.mark.asyncio
async def test_identificador_de_numero_meta_error_de_red(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(meta_wa_access_token="EAA-de-mentira", meta_wa_phone_number_id="998877")

    _falsear_get(monkeypatch, _corta_la_red)
    try:
        cuerpo = await _probar(token, "meta_wa_phone_number_id")
        assert cuerpo["ok"] is False
        assert "red" in cuerpo["message"].lower()
        assert "EAA-de-mentira" not in cuerpo["message"]
    finally:
        await _limpiar(META_KEYS, ids)


@pytest.mark.asyncio
async def test_cuenta_de_whatsapp_business_con_plantillas(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(meta_wa_access_token="EAA-de-mentira", meta_wa_business_account_id="5544")

    def responder(url, kw):
        assert "/5544/message_templates" in url
        return httpx.Response(200, json={"data": [{"name": "recordatorio_cita"}]})

    _falsear_get(monkeypatch, responder)
    try:
        cuerpo = await _probar(token, "meta_wa_business_account_id")
        assert cuerpo["ok"] is True
        assert "Meta OK" in cuerpo["message"]
    finally:
        await _limpiar(META_KEYS, ids)


@pytest.mark.asyncio
async def test_cuenta_de_whatsapp_business_sin_plantillas(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(meta_wa_access_token="EAA-de-mentira", meta_wa_business_account_id="5544")

    _falsear_get(monkeypatch, lambda url, kw: httpx.Response(200, json={"data": []}))
    try:
        cuerpo = await _probar(token, "meta_wa_business_account_id")
        assert cuerpo["ok"] is False
        assert "plantilla" in cuerpo["message"]
    finally:
        await _limpiar(META_KEYS, ids)


@pytest.mark.asyncio
async def test_cuenta_de_whatsapp_business_rechazada(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(meta_wa_access_token="EAA-de-mentira", meta_wa_business_account_id="0000")

    _falsear_get(monkeypatch, lambda url, kw: httpx.Response(403, json={"error": {"code": 200}}))
    try:
        cuerpo = await _probar(token, "meta_wa_business_account_id")
        assert cuerpo["ok"] is False
        assert "no reconoce" in cuerpo["message"]
    finally:
        await _limpiar(META_KEYS, ids)


@pytest.mark.asyncio
async def test_cuenta_de_whatsapp_business_error_de_red(monkeypatch):
    token, ids = await _admin_token()
    await _guardar(meta_wa_access_token="EAA-de-mentira", meta_wa_business_account_id="5544")

    _falsear_get(monkeypatch, _corta_la_red)
    try:
        cuerpo = await _probar(token, "meta_wa_business_account_id")
        assert cuerpo["ok"] is False
        assert "red" in cuerpo["message"].lower()
    finally:
        await _limpiar(META_KEYS, ids)
