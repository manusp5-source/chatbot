"""Endpoints OAuth para conectar APIs externas (F8b).

Por ahora soporta Google (Calendar / Gmail / Drive). El flujo:

  1. Admin abre `/admin/connections` → click "Conectar Google Calendar".
  2. Frontend llama `POST /admin/oauth/google/start?provider=google_calendar`,
     recibe la URL de Google y abre nueva pestaña con esa URL.
  3. El administrador acepta los permisos en Google.
  4. Google redirige a `/api/v1/oauth/google/callback?code=...&state=...`.
  5. El callback intercambia el code por tokens, los guarda cifrados en
     ExternalAPI y devuelve un pequeño HTML que cierra la pestaña.

El `state` es un token aleatorio guardado temporalmente en Redis (TTL 10
min) para validar que el callback es del flujo que arrancamos, Y en una cookie
del navegador que lo arrancó (ver `_STATE_COOKIE` más abajo).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets

from fastapi import APIRouter, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse

from app.core.config import settings
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.services.google_oauth import (
    SCOPES_BY_PROVIDER,
    build_auth_url,
    exchange_code,
    generate_state,
    save_tokens,
)
from app.services import instagram_oauth as ig_oauth

logger = get_logger(__name__)

router = APIRouter(prefix="/oauth", tags=["oauth"])

_STATE_PREFIX = "oauth:state:"
_STATE_TTL = 600  # 10 min

# El `state` NO basta con guardarlo en Redis: Redis es global, así que el
# callback aceptaba CUALQUIER state vivo lo hubiera pedido quien lo hubiera
# pedido. Con un state filtrado (historial, Referer, una pestaña abandonada, un
# pantallazo del panel) un tercero puede terminar el baile desde SU navegador
# con SU cuenta: guardaríamos las credenciales del atacante como las del
# negocio y el bot acabaría leyendo/escribiendo en su Gmail/Calendar/Drive o
# atado a su Instagram. Por eso el state viaja ADEMÁS en esta cookie, que solo
# tiene el navegador que inició el flujo, y el callback exige que coincida.
# Mismo patrón que el login con Google en `auth.py`: HttpOnly (invisible a JS),
# SameSite=Lax (el navegador sí la manda en la vuelta de Google/Meta, que es
# una navegación GET de primer nivel), Secure en producción y vida corta (la
# misma que el state en Redis).
_STATE_COOKIE = "oauth_state"
# Acotada a la ruta de los callbacks: no viaja en el resto de peticiones.
_STATE_COOKIE_PATH = "/api/v1/oauth"


def set_state_cookie(response: Response, state: str) -> None:
    """Ata el `state` al navegador que arranca el flujo.

    La llaman los endpoints de arranque (`/admin/oauth/{google,instagram}/start`)
    sobre su propia respuesta; el callback exige después que la cookie coincida.
    """
    response.set_cookie(
        _STATE_COOKIE,
        state,
        max_age=_STATE_TTL,
        httponly=True,
        # En desarrollo la API va por http://localhost: con Secure el navegador
        # no la mandaría y conectar Google dejaría de funcionar en local.
        secure=settings.is_production,
        samesite="lax",
        path=_STATE_COOKIE_PATH,
    )


def _state_matches_browser(request: Request, state: str) -> bool:
    """¿Arrancó ESTE navegador el flujo? compare_digest para no filtrar el
    state por tiempos."""
    cookie_state = request.cookies.get(_STATE_COOKIE)
    return bool(cookie_state) and secrets.compare_digest(cookie_state, state)


def _finish_oauth(
    *, ok: bool = False, provider: str = "", error_msg: str = ""
) -> HTMLResponse:
    """Cierra el flujo: HTML de siempre + BORRA la cookie de state.

    Se usa en TODAS las salidas de los callbacks (éxito y error) para que no
    quede un state reutilizable en el navegador.
    """
    response = _close_window_html(ok=ok, provider=provider, error_msg=error_msg)
    response.delete_cookie(_STATE_COOKIE, path=_STATE_COOKIE_PATH)
    return response


@router.get("/instagram/callback", include_in_schema=False)
async def instagram_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
) -> HTMLResponse:
    """Callback público de Instagram Business Login OAuth."""
    if error:
        return _finish_oauth(error_msg=f"Instagram devolvió error: {error}")
    if not code or not state:
        return _finish_oauth(error_msg="Faltan parámetros 'code' o 'state'")

    # 1) ¿Lo pidió ESTE navegador? Se comprueba ANTES que nada: si no cuadra,
    #    ni siquiera canjeamos el `code` con Meta ni consumimos el state (el
    #    flujo legítimo todavía tiene que poder terminar).
    if not _state_matches_browser(request, state):
        logger.warning("ig.callback.state_mismatch")
        return _finish_oauth(
            error_msg="Este enlace no corresponde a la conexión que iniciaste en este navegador. Reintenta desde el panel."
        )

    redis = get_redis()
    key = _STATE_PREFIX + state
    marker = await redis.get(key)
    if not marker:
        return _finish_oauth(
            error_msg="El estado del OAuth expiró o es inválido. Reintenta desde el panel."
        )
    await redis.delete(key)

    try:
        tokens = await ig_oauth.exchange_code(code)
        await ig_oauth.save_tokens(tokens)
        # Asegura que existe un Channel(instagram_dm) con app_secret y
        # verify_token rellenos para que el webhook pueda validarse.
        # app_secret = el client_secret de la app de Meta (lo tenemos en
        # credentials). verify_token lo generamos si no había.
        await _ensure_instagram_channel()
    except Exception as e:
        logger.error("ig.callback.error", error=str(e))
        return _finish_oauth(error_msg="No se pudo completar la conexión. Revisa los logs e inténtalo de nuevo.")

    return _finish_oauth(ok=True, provider="instagram_business")


async def _ensure_instagram_channel() -> None:
    """Tras OAuth, garantiza que existe Channel(instagram_dm) con app_secret
    + verify_token rellenos. Si no hay, los pone con los valores correctos."""
    import secrets
    from sqlalchemy import select as _select
    from app.db.session import db_session
    from app.models.channel import Channel, ChannelType
    from app.services.channel_secrets import (
        apply_channel_values,
        channel_config,
        secrets_unreadable,
    )
    from app.services.credentials import get_credential

    # Recupera app_secret de la app de Meta (es la misma cred que el OAuth)
    app_secret = await get_credential("instagram_oauth_client_secret") or ""

    async with db_session() as db:
        ch = (
            await db.execute(
                _select(Channel).where(Channel.type == ChannelType.instagram_dm).limit(1)
            )
        ).scalar_one_or_none()
        if ch:
            # Si las claves guardadas no se pueden descifrar, NO se toca nada.
            # Aquí no hay nadie mirando (esto corre solo al volver de Meta), y
            # "no puedo leerlo" se parece mucho a "no hay nada": si se
            # confunden, se acuña un verify_token nuevo que Meta no conoce y el
            # webhook deja de verificarse, además de pisar el app_secret bueno.
            if secrets_unreadable(ch):
                logger.error(
                    "ig.channel.secrets_unreadable",
                    channel_id=str(ch.id),
                    detalle=(
                        "El canal de Instagram tiene credenciales que no se pueden "
                        "descifrar (¿ENCRYPTION_KEY rotada?). No se toca nada: "
                        "vuelve a conectarlo desde Conexiones."
                    ),
                )
                return
            actual = channel_config(ch)
            nuevos: dict[str, str] = {}
            if not actual.get("app_secret") and app_secret:
                nuevos["app_secret"] = app_secret
            if not actual.get("verify_token"):
                nuevos["verify_token"] = secrets.token_urlsafe(32)
            if nuevos:
                apply_channel_values(ch, nuevos)
                ch.enabled = True
                await db.commit()
        else:
            ch = Channel(
                type=ChannelType.instagram_dm,
                name="Instagram DM",
                enabled=True,
                config={},
            )
            apply_channel_values(
                ch,
                {
                    "app_secret": app_secret,
                    "verify_token": secrets.token_urlsafe(32),
                    "via": "business_login_oauth",
                },
            )
            db.add(ch)
            await db.commit()


@router.get("/google/callback", include_in_schema=False)
async def google_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
) -> HTMLResponse:
    """Callback público de Google OAuth.

    Si todo va bien, guarda los tokens cifrados y devuelve un HTML que
    cierra la pestaña sola. Si hay error, lo muestra para que el administrador sepa.
    """
    if error:
        return _finish_oauth(error_msg=f"Google devolvió error: {error}")
    if not code or not state:
        return _finish_oauth(error_msg="Faltan parámetros 'code' o 'state'")

    # 1) ¿Lo pidió ESTE navegador? Se comprueba ANTES que nada: si no cuadra,
    #    ni siquiera canjeamos el `code` con Google ni consumimos el state (el
    #    flujo legítimo todavía tiene que poder terminar).
    if not _state_matches_browser(request, state):
        logger.warning("google.callback.state_mismatch")
        return _finish_oauth(
            error_msg="Este enlace no corresponde a la conexión que iniciaste en este navegador. Reintenta desde el panel."
        )

    redis = get_redis()
    key = _STATE_PREFIX + state
    provider = await redis.get(key)
    if not provider:
        return _finish_oauth(
            error_msg="El estado del OAuth expiró o es inválido. Reintenta desde el panel."
        )
    await redis.delete(key)
    provider_str = provider if isinstance(provider, str) else provider.decode()
    if provider_str not in SCOPES_BY_PROVIDER:
        return _finish_oauth(error_msg=f"Provider desconocido: {provider_str}")

    try:
        tokens = await exchange_code(code)
        if not tokens.refresh_token:
            # Si Google no nos dio refresh_token, no podremos renovar después.
            # Pasa cuando el usuario ya había consentido antes y no se ha
            # revocado en su cuenta. Por eso pedimos prompt=consent en
            # build_auth_url, pero por si acaso.
            logger.warning("google.callback.no_refresh_token", provider=provider_str)
        await save_tokens(provider_str, tokens)
    except Exception as e:
        logger.error("google.callback.error", error=str(e))
        return _finish_oauth(error_msg="No se pudo completar la conexión. Revisa los logs e inténtalo de nuevo.")

    return _finish_oauth(ok=True, provider=provider_str)


def _close_window_html(
    *, ok: bool = False, provider: str = "", error_msg: str = ""
) -> HTMLResponse:
    """Devuelve un HTML que cierra solo la pestaña del OAuth + avisa al opener.

    Si la pestaña fue abierta por window.open() del panel, podemos enviar
    un postMessage al opener para que refresque automáticamente.

    Seguridad:
      - Todo texto interpolado (provider, error_msg — `error` llega del query
        string) se escapa con html.escape() → sin XSS reflejado.
      - La CSP global del middleware (`default-src 'none'`) bloquearía el
        <script>/<style> inline de esta página (la pestaña no se cerraba y el
        postMessage nunca llegaba al panel). Emitimos aquí una CSP propia que
        permite SOLO nuestro inline vía nonce por respuesta; el middleware no
        la pisa (solo añade headers que falten).
      - COOP: el middleware pone 'same-origin', que rompería window.opener al
        volver de Google/Meta (otro origen) → el postMessage al panel se
        perdería. Esta página existe justamente para hablar con su opener, así
        que emite 'unsafe-none' (el comportamiento por defecto del navegador).
    """
    nonce = secrets.token_urlsafe(16)
    title = "Conectado" if ok else "Error"
    safe_provider = html.escape(provider)
    body = (
        f"<h2 class='ok'>Conexión OK · {safe_provider}</h2>"
        "<p>Esta pestaña se cerrará sola en 2 segundos. Vuelve al panel.</p>"
        if ok
        else f"<h2 class='err'>Error</h2><p>{html.escape(error_msg)}</p>"
        "<p>Cierra esta pestaña y reintenta desde el panel.</p>"
    )
    # json.dumps + escape de '<' → ni comillas ni un provider malicioso pueden
    # romper el contexto del <script> (p.ej. con "</script>").
    provider_js = json.dumps(provider).replace("<", "\\u003c")
    js_payload = "{type:'chatbot.oauth.ok',provider:" + provider_js + "}"
    script = f"""
      <script nonce="{nonce}">
        try {{
          if (window.opener) {{
            window.opener.postMessage({js_payload}, '*');
          }}
        }} catch (e) {{ /* ignore */ }}
        setTimeout(() => {{ try {{ window.close(); }} catch (e) {{}} }}, 2000);
      </script>
    """ if ok else ""
    doc = f"""
    <!doctype html><html><head><meta charset="utf-8"><title>{title} · {settings.APP_NAME}</title>
    <style nonce="{nonce}">
      body {{ font-family: system-ui, -apple-system, sans-serif; background: #F7F2E9;
              color: #1A1612; padding: 40px 20px; text-align: center; }}
      h2 {{ font-family: Georgia, serif; font-weight: 400; margin-top: 0; }}
      h2.ok {{ color: #16A085; }}
      h2.err {{ color: #DC4B3C; }}
      p {{ color: #6A604F; }}
    </style></head>
    <body>{body}{script}</body></html>
    """
    return HTMLResponse(
        doc,
        headers={
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; "
                f"style-src 'nonce-{nonce}'; base-uri 'none'; frame-ancestors 'none'"
            ),
            "Cross-Origin-Opener-Policy": "unsafe-none",
        },
    )


# ============================ Meta data compliance ============================
# Endpoints que Meta EXIGE para publicar la app (ponerla en modo "En vivo"):
#   - Deauthorize callback: se llama cuando alguien quita la app de su cuenta.
#   - Data deletion request: se llama cuando un usuario pide borrar sus datos.
# Ambos reciben un `signed_request` firmado con el App Secret (HMAC-SHA256).
# Docs: https://developers.facebook.com/docs/development/create-an-app/app-dashboard/data-deletion-callback


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _parse_signed_request(signed_request: str, app_secret: str) -> dict | None:
    """Valida y decodifica el signed_request de Meta. None si la firma falla."""
    try:
        sig_b64, payload_b64 = signed_request.split(".", 1)
    except ValueError:
        return None
    try:
        expected = hmac.new(
            app_secret.encode(), payload_b64.encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64url_decode(sig_b64), expected):
            return None
        return json.loads(_b64url_decode(payload_b64))
    except Exception:
        return None


async def _get_meta_app_secret() -> str | None:
    """App Secret de Meta: del Channel IG (donde lo usa el webhook) con
    fallback a la credencial instagram_oauth_client_secret."""
    from sqlalchemy import select as _select

    from app.db.session import db_session
    from app.models.channel import Channel, ChannelType
    from app.services.channel_secrets import channel_config
    from app.services.credentials import get_credential

    async with db_session() as db:
        ch = (
            await db.execute(
                _select(Channel).where(Channel.type == ChannelType.instagram_dm).limit(1)
            )
        ).scalar_one_or_none()
    secret = channel_config(ch).get("app_secret") if ch else None
    if not secret:
        secret = await get_credential("instagram_oauth_client_secret")
    return secret or None


@router.post("/instagram/deauthorize", include_in_schema=False)
async def instagram_deauthorize(signed_request: str = Form(...)) -> dict:
    """Meta llama aquí cuando alguien quita la app de su cuenta de Instagram.
    Revocamos el token almacenado de esa cuenta."""
    secret = await _get_meta_app_secret()
    if not secret:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "app_secret no configurado")
    data = _parse_signed_request(signed_request, secret)
    if data is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "signed_request inválido")
    user_id = str(data.get("user_id") or "")

    from sqlalchemy import select as _select

    from app.db.session import db_session
    from app.models.external_api import ExternalAPI

    async with db_session() as db:
        api = (
            await db.execute(
                _select(ExternalAPI).where(ExternalAPI.provider == "instagram_business")
            )
        ).scalar_one_or_none()
        stored_uid = (api.extra or {}).get("instagram_user_id") if api else None
        # Solo revoca si el deauth corresponde a la cuenta conectada (o si no
        # sabemos a quién pertenece el token guardado).
        if api and (not stored_uid or stored_uid == user_id):
            api.is_active = False
            api.credentials_encrypted = None
            await db.commit()
    logger.info("ig.deauthorize", uid=(user_id[:6] + "…") if user_id else "")
    return {"ok": True}


@router.post("/instagram/data-deletion", include_in_schema=False)
async def instagram_data_deletion(signed_request: str = Form(...)) -> dict:
    """Meta llama aquí cuando un usuario solicita el borrado de sus datos.
    Borramos el contacto de Instagram y su histórico (cascade) y devolvemos
    el confirmation_code + URL de estado que Meta espera."""
    secret = await _get_meta_app_secret()
    if not secret:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "app_secret no configurado")
    data = _parse_signed_request(signed_request, secret)
    if data is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "signed_request inválido")
    user_id = str(data.get("user_id") or "")
    code = secrets.token_hex(8)

    deleted = 0
    if user_id:
        from sqlalchemy import select as _select

        from app.db.session import db_session
        from app.models.contact import Contact

        try:
            async with db_session() as db:
                contact = (
                    await db.execute(
                        _select(Contact).where(Contact.telefono == f"ig:{user_id}")
                    )
                ).scalar_one_or_none()
                if contact:
                    # Borrado COMPLETO (RGPD Art. 17): cascade + ficheros en
                    # disco + huecos + envíos + Redis. Igual que el borrado
                    # manual desde el panel.
                    from app.services.data_erasure import erase_contact, finish_erasure

                    report = await erase_contact(db, contact.id)
                    await db.commit()
                    # La limpieza de Redis va DESPUÉS del commit (ver
                    # data_erasure.finish_erasure): o se confirma todo, o nada.
                    await finish_erasure(report)
                    deleted = 1
        except Exception as e:
            logger.error("ig.data_deletion.delete_failed", error=str(e))

    logger.info("ig.data_deletion", code=code, deleted=deleted)
    base = settings.APP_BASE_URL.rstrip("/")
    return {
        "url": f"{base}/api/v1/oauth/instagram/data-deletion/status?code={code}",
        "confirmation_code": code,
    }


@router.get("/instagram/data-deletion/status", include_in_schema=False)
async def instagram_data_deletion_status(
    code: str | None = Query(None),
) -> HTMLResponse:
    """Página pública de estado del borrado (Meta exige una URL consultable)."""
    safe = html.escape(code or "—")
    # Misma situación que _close_window_html: la CSP global (default-src 'none')
    # bloquearía el <style> inline → CSP propia con nonce (aquí solo estilos).
    nonce = secrets.token_urlsafe(16)
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8">
    <title>Eliminación de datos · {settings.APP_NAME}</title>
    <style nonce="{nonce}">body{{font-family:system-ui,-apple-system,sans-serif;background:#F7F2E9;
    color:#1A1612;padding:40px 20px;text-align:center}}
    h2{{font-family:Georgia,serif;font-weight:400}}p{{color:#6A604F}}
    code{{background:#EFE8DA;padding:2px 8px;border-radius:6px}}</style></head>
    <body><h2>Solicitud de eliminación de datos</h2>
    <p>Tu solicitud con código <code>{safe}</code> ha sido recibida y procesada.</p>
    <p>Los datos asociados a tu cuenta de Instagram han sido eliminados de este sistema.</p>
    </body></html>""",
        headers={
            "Content-Security-Policy": (
                f"default-src 'none'; style-src 'nonce-{nonce}'; "
                "base-uri 'none'; frame-ancestors 'none'"
            ),
        },
    )
