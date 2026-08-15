"""OAuth 2.0 con Google (Calendar + Gmail + Drive) — F8b.

Flujo:
  1. `build_auth_url(provider, state)` → URL de Google con scopes específicos.
  2. Google redirige a /api/v1/oauth/google/callback?code=...&state=...
  3. `exchange_code(code)` → tokens (access + refresh).
  4. Guardamos `refresh_token` cifrado en `ExternalAPI.credentials_encrypted`.
  5. Cuando un tool del agente necesita la API, `get_access_token(provider)`
     usa el refresh_token para obtener un access_token fresco (Google los
     hace expirar en 1h).

Cada producto Google (Calendar, Gmail, Drive) es una `ExternalAPI` distinta
para que el administrador pueda conectar/revocar cada uno independiente (decisión
suya en el plan multicanal). Pueden compartir la misma cuenta Google de
fondo — solo cambian los scopes.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.core.encryption import get_encryption_service
from app.core.logging import get_logger
from app.db.session import db_session
from app.models.external_api import ExternalAPI

logger = get_logger(__name__)

GOOGLE_AUTH_BASE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Scopes por producto. El usuario consiente cada uno explícitamente.
SCOPES_BY_PROVIDER: dict[str, list[str]] = {
    "google_calendar": [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.events",
    ],
    "google_gmail": [
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.send",
    ],
    "google_drive": [
        "https://www.googleapis.com/auth/drive.file",
    ],
}


@dataclass
class GoogleTokens:
    access_token: str
    refresh_token: str | None  # solo viene en el primer consentimiento
    expires_at: datetime
    scope: str


def _redirect_uri() -> str:
    if settings.GOOGLE_OAUTH_REDIRECT_URI:
        return settings.GOOGLE_OAUTH_REDIRECT_URI
    base = settings.APP_BASE_URL.rstrip("/")
    return f"{base}/api/v1/oauth/google/callback"


async def _get_oauth_app_credentials() -> tuple[str | None, str | None]:
    """Lee client_id/secret desde la tabla credentials (editable desde panel),
    con fallback a env vars para retrocompatibilidad."""
    from app.services.credentials import get_credential
    client_id = await get_credential("google_oauth_client_id") or settings.GOOGLE_OAUTH_CLIENT_ID
    client_secret = await get_credential("google_oauth_client_secret") or settings.GOOGLE_OAUTH_CLIENT_SECRET
    return client_id or None, client_secret or None


async def build_auth_url(provider: str, state: str) -> str:
    """Construye la URL de Google para iniciar el OAuth dance.

    `state` es un token aleatorio que validaremos en el callback (anti-CSRF).
    """
    if provider not in SCOPES_BY_PROVIDER:
        raise ValueError(f"Provider Google desconocido: {provider}")
    client_id, _ = await _get_oauth_app_credentials()
    if not client_id:
        raise RuntimeError(
            "Google OAuth no configurado. Ve a /admin/credentials y rellena "
            "google_oauth_client_id y google_oauth_client_secret (de tu app en Google Cloud Console)."
        )
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES_BY_PROVIDER[provider]),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_AUTH_BASE}?{urlencode(params)}"


def generate_state() -> str:
    return secrets.token_urlsafe(32)


async def exchange_code(code: str) -> GoogleTokens:
    """Intercambia el `code` del callback por tokens reales."""
    client_id, client_secret = await _get_oauth_app_credentials()
    if not (client_id and client_secret):
        raise RuntimeError("Credenciales Google OAuth no configuradas")
    data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": _redirect_uri(),
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(GOOGLE_TOKEN_URL, data=data)
        if r.status_code >= 400:
            logger.error("google.token.exchange_failed", status=r.status_code, body=r.text[:200])
            r.raise_for_status()
        payload = r.json()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(payload.get("expires_in", 3600)) - 60)
    return GoogleTokens(
        access_token=str(payload["access_token"]),
        refresh_token=payload.get("refresh_token"),
        expires_at=expires_at,
        scope=str(payload.get("scope", "")),
    )


async def refresh_access_token(refresh_token: str) -> GoogleTokens:
    """Pide un access_token nuevo con el refresh_token."""
    client_id, client_secret = await _get_oauth_app_credentials()
    if not (client_id and client_secret):
        raise RuntimeError("Credenciales Google OAuth no configuradas")
    data = {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(GOOGLE_TOKEN_URL, data=data)
        r.raise_for_status()
        payload = r.json()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(payload.get("expires_in", 3600)) - 60)
    return GoogleTokens(
        access_token=str(payload["access_token"]),
        refresh_token=refresh_token,  # se mantiene
        expires_at=expires_at,
        scope=str(payload.get("scope", "")),
    )


async def save_tokens(provider: str, tokens: GoogleTokens) -> None:
    """Persiste los tokens cifrados en ExternalAPI(provider=..., name=provider).

    Si ya existe esa entrada (re-conexión), se actualizan los tokens.
    """
    enc = get_encryption_service()
    blob = {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "expires_at": tokens.expires_at.isoformat(),
        "scope": tokens.scope,
    }
    encrypted = enc.encrypt(json.dumps(blob))
    async with db_session() as db:
        existing = (
            await db.execute(
                select(ExternalAPI).where(
                    ExternalAPI.provider == provider, ExternalAPI.name == provider
                )
            )
        ).scalar_one_or_none()
        if existing:
            # Si Google solo manda access_token (refresh ya teníamos), conservamos
            # el refresh anterior.
            if not tokens.refresh_token:
                prev = json.loads(enc.decrypt(existing.credentials_encrypted)) if existing.credentials_encrypted else {}
                if prev.get("refresh_token"):
                    blob["refresh_token"] = prev["refresh_token"]
                    encrypted = enc.encrypt(json.dumps(blob))
            existing.credentials_encrypted = encrypted
            existing.is_active = True
            # Al reconectar se limpia el motivo de fallo anterior: si no, el
            # panel seguiría enseñando "permiso revocado" con la cuenta ya
            # arreglada.
            new_extra = {
                **(existing.extra or {}),
                "connected_at": datetime.now(timezone.utc).isoformat(),
                "scope": tokens.scope,
            }
            new_extra.pop("auth_error", None)
            existing.extra = new_extra
        else:
            db.add(
                ExternalAPI(
                    provider=provider,
                    name=provider,
                    credentials_encrypted=encrypted,
                    is_active=True,
                    extra={
                        "connected_at": datetime.now(timezone.utc).isoformat(),
                        "scope": tokens.scope,
                    },
                )
            )
        await db.commit()


# ---------------------------------------------------------------------------
# Estado de la conexión (E3) — una cuenta desconectada NO puede fallar en
# silencio. Antes cualquier problema con el token devolvía None con un simple
# log dentro del contenedor: el poller dejaba de traer correo, la integración
# seguía marcada como activa y el panel seguía diciendo "Conectado".
# ---------------------------------------------------------------------------

# Códigos de fallo. Se distinguen porque la solución de cada uno es distinta:
#   no_connection     → nunca se conectó (o se borró la entrada).
#   decrypt_failed    → ENCRYPTION_KEY cambiada/rotada: hay que reconectar.
#   no_refresh_token  → Google no devolvió refresh_token (falta prompt=consent).
#   refresh_rejected  → Google RECHAZÓ el refresh (revocado, contraseña
#                       cambiada, app en pruebas caducada): reconexión obligada.
#   refresh_error     → fallo transitorio (red, 5xx): se reintentará solo.
AUTH_ERROR_NO_CONNECTION = "no_connection"
AUTH_ERROR_DECRYPT_FAILED = "decrypt_failed"
AUTH_ERROR_NO_REFRESH_TOKEN = "no_refresh_token"
AUTH_ERROR_REFRESH_REJECTED = "refresh_rejected"
AUTH_ERROR_REFRESH_ERROR = "refresh_error"

# Mensajes en cristiano para el panel: quien los lee no sabe qué es un
# refresh_token, necesita saber qué tiene que hacer.
_AUTH_ERROR_MESSAGES = {
    AUTH_ERROR_NO_CONNECTION: (
        "La cuenta de Google no está conectada. Ve a Conexiones y conéctala."
    ),
    AUTH_ERROR_DECRYPT_FAILED: (
        "No se pueden descifrar las credenciales guardadas (la clave de cifrado "
        "ha cambiado). Hay que volver a conectar la cuenta de Google."
    ),
    AUTH_ERROR_NO_REFRESH_TOKEN: (
        "La conexión se guardó sin permiso permanente. Hay que volver a "
        "conectar la cuenta de Google concediendo el acceso otra vez."
    ),
    AUTH_ERROR_REFRESH_REJECTED: (
        "Google ha rechazado el acceso (permiso revocado o contraseña "
        "cambiada). Hay que volver a conectar la cuenta."
    ),
    AUTH_ERROR_REFRESH_ERROR: (
        "No se ha podido renovar el acceso a Google (fallo temporal). Se "
        "reintentará solo; si persiste, vuelve a conectar la cuenta."
    ),
}

# Fallos DEFINITIVOS: no se arreglan solos, así que además de dejar constancia
# desactivamos la integración para que el panel deje de decir "Conectado".
_PERMANENT_AUTH_ERRORS = {
    AUTH_ERROR_DECRYPT_FAILED,
    AUTH_ERROR_NO_REFRESH_TOKEN,
    AUTH_ERROR_REFRESH_REJECTED,
}


def auth_error_message(code: str) -> str:
    """Texto explicativo de un código de fallo de autenticación."""
    return _AUTH_ERROR_MESSAGES.get(code, "Fallo de autenticación con Google.")


async def _record_auth_failure(provider: str, code: str, detail: str = "") -> None:
    """Deja constancia VISIBLE de que la conexión con Google está rota.

    Escribe el motivo en `ExternalAPI.extra["auth_error"]` (que el endpoint
    /admin/external-apis ya devuelve tal cual al panel) y lo empuja a
    Monitorización. Si el fallo es definitivo, marca la integración como
    inactiva: seguir mostrándola "Conectado" es justo lo que hacía que los
    correos dejaran de llegar sin que nadie se enterara.

    Best-effort: nunca lanza (esto se llama desde el camino de lectura de
    tokens; romperlo dejaría el sistema peor de lo que ya está).
    """
    message = auth_error_message(code)
    logger.error("google.auth_failed", provider=provider, code=code, detail=detail[:200])
    try:
        from app.services.runtime_logs import push_runtime_log

        await push_runtime_log(
            level="error",
            event="google.auth_failed",
            message=f"{provider}: {message}",
            provider=provider,
            code=code,
        )
    except Exception:  # noqa: BLE001 — el log nunca puede romper el flujo
        pass
    if code == AUTH_ERROR_NO_CONNECTION:
        # No hay fila donde anotar nada.
        return
    try:
        async with db_session() as db:
            api = (
                await db.execute(
                    select(ExternalAPI).where(
                        ExternalAPI.provider == provider, ExternalAPI.name == provider
                    )
                )
            ).scalar_one_or_none()
            if not api:
                return
            api.extra = {
                **(api.extra or {}),
                "auth_error": {
                    "code": code,
                    "message": message,
                    "detail": detail[:300],
                    "at": datetime.now(timezone.utc).isoformat(),
                },
            }
            if code in _PERMANENT_AUTH_ERRORS:
                api.is_active = False
            await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("google.auth_failed.record_error", error=str(e))


async def _clear_auth_failure(provider: str) -> None:
    """Borra la marca de fallo cuando el token vuelve a funcionar."""
    try:
        async with db_session() as db:
            api = (
                await db.execute(
                    select(ExternalAPI).where(
                        ExternalAPI.provider == provider, ExternalAPI.name == provider
                    )
                )
            ).scalar_one_or_none()
            if not api or "auth_error" not in (api.extra or {}):
                return
            extra = dict(api.extra or {})
            extra.pop("auth_error", None)
            api.extra = extra
            await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("google.auth_clear_error", error=str(e))


def _is_permanent_refresh_error(exc: Exception) -> bool:
    """¿El rechazo del refresh es definitivo o un fallo pasajero?

    Google contesta 400/401 (`invalid_grant`) cuando el permiso ya no vale:
    revocado, contraseña cambiada, app en modo prueba caducada. Cualquier otra
    cosa (timeout, 5xx, DNS) es transitoria y se arregla sola en el siguiente
    sondeo — no hay que desconectar la cuenta por un corte de red.
    """
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    return status in (400, 401)


async def get_access_token(provider: str) -> str | None:
    """Devuelve un access_token válido (renueva si está expirado).

    Devuelve None si la conexión no sirve, pero NUNCA en silencio: cada motivo
    queda registrado en la propia integración y en Monitorización (ver
    `_record_auth_failure`).
    """
    enc = get_encryption_service()
    async with db_session() as db:
        api = (
            await db.execute(
                select(ExternalAPI).where(
                    ExternalAPI.provider == provider,
                    ExternalAPI.name == provider,
                    ExternalAPI.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
    if not api or not api.credentials_encrypted:
        await _record_auth_failure(provider, AUTH_ERROR_NO_CONNECTION)
        return None
    try:
        blob = json.loads(enc.decrypt(api.credentials_encrypted))
    except Exception as e:
        await _record_auth_failure(provider, AUTH_ERROR_DECRYPT_FAILED, str(e))
        return None
    expires_at = datetime.fromisoformat(blob["expires_at"])
    if expires_at > datetime.now(timezone.utc):
        return str(blob["access_token"])
    refresh_token = blob.get("refresh_token")
    if not refresh_token:
        await _record_auth_failure(provider, AUTH_ERROR_NO_REFRESH_TOKEN)
        return None
    try:
        fresh = await refresh_access_token(refresh_token)
    except Exception as e:
        code = (
            AUTH_ERROR_REFRESH_REJECTED
            if _is_permanent_refresh_error(e)
            else AUTH_ERROR_REFRESH_ERROR
        )
        await _record_auth_failure(provider, code, str(e))
        return None
    await save_tokens(provider, fresh)
    # Volvió a funcionar: quitamos la marca de fallo para que el panel no se
    # quede enseñando un error ya resuelto.
    await _clear_auth_failure(provider)
    return fresh.access_token
