"""OAuth 2.0 con Instagram Business Login (F8c).

Flujo de Instagram with Instagram Login (la API moderna, sin Facebook Page):

  1. `build_auth_url(state)` → URL de Instagram con los scopes IG.
  2. Usuario acepta en instagram.com → redirige a /oauth/instagram/callback.
  3. `exchange_code_for_short_lived` (1h de duración).
  4. `exchange_short_for_long_lived` (60 días, refrescable).
  5. Guardamos token long-lived cifrado en ExternalAPI(provider=instagram_business).
  6. En cada uso, `get_access_token()` refresca si está cerca de expirar.
  7. Para mandar mensajes: `POST https://graph.instagram.com/v22.0/me/messages`.

Docs: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login
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

PROVIDER = "instagram_business"

IG_AUTH_BASE = "https://www.instagram.com/oauth/authorize"
IG_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
IG_GRAPH_BASE = "https://graph.instagram.com"

SCOPES = [
    "instagram_business_basic",
    "instagram_business_manage_messages",
    "instagram_business_manage_comments",
]


@dataclass
class IGTokens:
    access_token: str
    user_id: str  # Instagram Business User ID
    expires_at: datetime
    scope: str


def _redirect_uri() -> str:
    if settings.INSTAGRAM_OAUTH_REDIRECT_URI:
        return settings.INSTAGRAM_OAUTH_REDIRECT_URI
    base = settings.APP_BASE_URL.rstrip("/")
    return f"{base}/api/v1/oauth/instagram/callback"


async def _get_oauth_app_credentials() -> tuple[str | None, str | None]:
    """Lee client_id/secret de la tabla credentials (panel) con fallback a env."""
    from app.services.credentials import get_credential
    client_id = (
        await get_credential("instagram_oauth_client_id")
        or settings.INSTAGRAM_OAUTH_CLIENT_ID
    )
    client_secret = (
        await get_credential("instagram_oauth_client_secret")
        or settings.INSTAGRAM_OAUTH_CLIENT_SECRET
    )
    return client_id or None, client_secret or None


async def build_auth_url(state: str) -> str:
    client_id, _ = await _get_oauth_app_credentials()
    if not client_id:
        raise RuntimeError(
            "Instagram OAuth no configurado. Ve a /admin/credentials y rellena "
            "instagram_oauth_client_id e instagram_oauth_client_secret (App ID + App Secret de Meta)."
        )
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": ",".join(SCOPES),
        "state": state,
    }
    return f"{IG_AUTH_BASE}?{urlencode(params)}"


def generate_state() -> str:
    return secrets.token_urlsafe(32)


async def exchange_code(code: str) -> IGTokens:
    """Intercambia el code por un token short-lived (1h) y luego lo cambia
    por uno long-lived (60 días) en un solo paso."""
    client_id, client_secret = await _get_oauth_app_credentials()
    if not (client_id and client_secret):
        raise RuntimeError("Credenciales Instagram OAuth no configuradas")

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Step 1: code → short-lived (form-urlencoded)
        r = await client.post(
            IG_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "redirect_uri": _redirect_uri(),
                "code": code,
            },
        )
        if r.status_code >= 400:
            logger.error("ig.token.exchange_failed", status=r.status_code, body=r.text[:300])
            r.raise_for_status()
        short = r.json()
        short_token = str(short["access_token"])
        user_id = str(short.get("user_id", ""))

        # Step 2: short → long-lived (60 días)
        r = await client.get(
            f"{IG_GRAPH_BASE}/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": client_secret,
                "access_token": short_token,
            },
        )
        if r.status_code >= 400:
            logger.error("ig.token.long_lived_failed", status=r.status_code, body=r.text[:300])
            r.raise_for_status()
        long = r.json()

    expires_in = int(long.get("expires_in", 60 * 24 * 3600))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60 * 60)
    return IGTokens(
        access_token=str(long["access_token"]),
        user_id=user_id,
        expires_at=expires_at,
        scope=",".join(SCOPES),
    )


async def refresh_access_token(token: str) -> IGTokens:
    """Refresca un long-lived token antes de que caduque. Solo funciona si el
    token tiene al menos 24h de vida usada (Instagram lo exige)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{IG_GRAPH_BASE}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": token},
        )
        if r.status_code >= 400:
            logger.error("ig.token.refresh_failed", status=r.status_code, body=r.text[:300])
            r.raise_for_status()
        data = r.json()
    expires_in = int(data.get("expires_in", 60 * 24 * 3600))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60 * 60)
    return IGTokens(
        access_token=str(data["access_token"]),
        user_id="",  # se conserva del save anterior
        expires_at=expires_at,
        scope=",".join(SCOPES),
    )


async def save_tokens(tokens: IGTokens) -> None:
    enc = get_encryption_service()
    blob = {
        "access_token": tokens.access_token,
        "expires_at": tokens.expires_at.isoformat(),
        "scope": tokens.scope,
    }
    encrypted = enc.encrypt(json.dumps(blob))
    async with db_session() as db:
        existing = (
            await db.execute(
                select(ExternalAPI).where(
                    ExternalAPI.provider == PROVIDER, ExternalAPI.name == PROVIDER
                )
            )
        ).scalar_one_or_none()
        # Mantenemos el user_id en `extra` para que el provider lo lea sin
        # tener que descifrar el blob cada vez.
        extra_update = {
            "connected_at": datetime.now(timezone.utc).isoformat(),
            "scope": tokens.scope,
        }
        if tokens.user_id:
            extra_update["instagram_user_id"] = tokens.user_id

        if existing:
            existing.credentials_encrypted = encrypted
            existing.is_active = True
            existing.extra = {**(existing.extra or {}), **extra_update}
        else:
            db.add(
                ExternalAPI(
                    provider=PROVIDER,
                    name=PROVIDER,
                    credentials_encrypted=encrypted,
                    is_active=True,
                    extra=extra_update,
                )
            )
        await db.commit()


async def get_access_token() -> str | None:
    """Devuelve un access_token válido. Si está cerca de expirar, refresca."""
    enc = get_encryption_service()
    async with db_session() as db:
        api = (
            await db.execute(
                select(ExternalAPI).where(
                    ExternalAPI.provider == PROVIDER,
                    ExternalAPI.name == PROVIDER,
                    ExternalAPI.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
    if not api or not api.credentials_encrypted:
        return None
    try:
        blob = json.loads(enc.decrypt(api.credentials_encrypted))
    except Exception:
        logger.error("ig.decrypt_failed")
        return None
    expires_at = datetime.fromisoformat(blob["expires_at"])
    # Si quedan más de 24h, devuelve el actual. Si queda menos, refresca.
    if expires_at - datetime.now(timezone.utc) > timedelta(hours=24):
        return str(blob["access_token"])
    try:
        fresh = await refresh_access_token(blob["access_token"])
    except Exception as e:
        logger.error("ig.refresh_failed", error=str(e))
        # Devuelve el actual aunque esté cerca de expirar — mejor responder
        # con error de IG que con None silencioso.
        return str(blob["access_token"])
    await save_tokens(fresh)
    return fresh.access_token


async def get_instagram_user_id() -> str | None:
    async with db_session() as db:
        api = (
            await db.execute(
                select(ExternalAPI).where(
                    ExternalAPI.provider == PROVIDER, ExternalAPI.name == PROVIDER
                )
            )
        ).scalar_one_or_none()
    if not api:
        return None
    return (api.extra or {}).get("instagram_user_id")
