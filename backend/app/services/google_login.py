"""Inicio de sesión con Google (OpenID Connect) — #1.

Esto es DISTINTO del OAuth de servicios (Calendar/Gmail/Drive) que vive en
`google_oauth.py`. Aquí NO conectamos ninguna API ni guardamos refresh
tokens: solo verificamos la identidad (el email) de quien entra, para
dejarle pasar al panel con su usuario YA existente.

Reutiliza el MISMO OAuth Client de Google (Client ID/Secret de Credenciales);
lo único distinto es:
  - el scope: `openid email profile` (no sensibles, sin verificación Google),
  - la redirect URI: `${APP_BASE_URL}/api/v1/auth/google/callback`
    (hay que añadirla a "URIs de redirección autorizados" del cliente OAuth).

Flujo:
  1. `build_login_url(state)` → URL de consentimiento de Google.
  2. Google redirige a /api/v1/auth/google/callback?code=...&state=...
  3. `fetch_identity(code)` → intercambia el code y lee el userinfo
     (email verificado + sub). Con eso, el endpoint busca el User y emite
     nuestro JWT de siempre.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.services.google_oauth import (
    GOOGLE_AUTH_BASE,
    GOOGLE_TOKEN_URL,
    _get_oauth_app_credentials,
)

logger = get_logger(__name__)

USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
LOGIN_SCOPES = ["openid", "email", "profile"]


@dataclass
class GoogleIdentity:
    email: str
    email_verified: bool
    sub: str
    name: str | None


def login_redirect_uri() -> str:
    """Redirect URI del LOGIN (distinta de la de servicios en google_oauth)."""
    if settings.GOOGLE_LOGIN_REDIRECT_URI:
        return settings.GOOGLE_LOGIN_REDIRECT_URI
    base = settings.APP_BASE_URL.rstrip("/")
    return f"{base}/api/v1/auth/google/callback"


async def build_login_url(state: str) -> str:
    """URL de Google para iniciar el login. `state` es anti-CSRF."""
    client_id, _ = await _get_oauth_app_credentials()
    if not client_id:
        raise RuntimeError(
            "Google OAuth no configurado. Rellena google_oauth_client_id y "
            "google_oauth_client_secret en Credenciales."
        )
    params = {
        "client_id": client_id,
        "redirect_uri": login_redirect_uri(),
        "response_type": "code",
        "scope": " ".join(LOGIN_SCOPES),
        # Login no necesita refresh tokens (no acceso offline a APIs).
        "access_type": "online",
        # Deja elegir cuenta si hay varias sesiones Google abiertas.
        "prompt": "select_account",
        "state": state,
    }
    return f"{GOOGLE_AUTH_BASE}?{urlencode(params)}"


async def fetch_identity(code: str) -> GoogleIdentity:
    """Intercambia el `code` y devuelve la identidad verificada del usuario.

    El access_token se obtiene en una llamada servidor-a-servidor directa a
    Google (TLS), así que el userinfo que devuelve es de confianza sin tener
    que validar la firma del id_token por separado.
    """
    client_id, client_secret = await _get_oauth_app_credentials()
    if not (client_id and client_secret):
        raise RuntimeError("Credenciales Google OAuth no configuradas")
    data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": login_redirect_uri(),
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(GOOGLE_TOKEN_URL, data=data)
        if r.status_code >= 400:
            logger.error("google.login.exchange_failed", status=r.status_code, body=r.text[:200])
            r.raise_for_status()
        access_token = str(r.json()["access_token"])
        ui = await client.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        ui.raise_for_status()
        info = ui.json()
    return GoogleIdentity(
        email=str(info.get("email", "")).lower(),
        email_verified=bool(info.get("email_verified", False)),
        sub=str(info.get("sub", "")),
        name=info.get("name"),
    )
