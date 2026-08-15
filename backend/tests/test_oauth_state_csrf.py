"""El `state` del OAuth de servicios (Google / Instagram) tiene que estar atado
AL NAVEGADOR, igual que ya lo está el del login con Google (tanda 1).

El fallo que cubre este test: los callbacks de `/api/v1/oauth/...` solo
comprobaban que el `state` existiera en Redis. Como Redis es global, valía
CUALQUIER state vivo, lo hubiera pedido quien lo hubiera pedido. Con un state
filtrado (historial, Referer, una pestaña abandonada, un pantallazo del panel)
un tercero puede terminar el baile de OAuth desde SU navegador con SU cuenta de
Google/Instagram: el sistema guarda ESAS credenciales como las del negocio, y a
partir de ahí el bot lee y escribe en el Gmail / Calendar / Drive del atacante,
o queda conectado a su cuenta de Instagram.

El arreglo (mismo patrón que `auth.py`): el `state` viaja también en una cookie
HttpOnly de vida corta que solo tiene el navegador que inició el flujo, y el
callback exige que coincida (`secrets.compare_digest`).

Sin BD: todos los casos probados se cortan antes de tocar Postgres.
"""
from __future__ import annotations

import pytest
from starlette.requests import Request


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


def _request(path: str, cookies: dict[str, str] | None = None) -> Request:
    headers = []
    if cookies:
        raw = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.append((b"cookie", raw.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": headers,
            "query_string": b"",
            "client": ("203.0.113.9", 51234),
            "scheme": "https",
            "server": ("api.test", 443),
        }
    )


def _set_cookies(response) -> dict[str, str]:
    out = {}
    for raw in response.headers.getlist("set-cookie"):
        out[raw.split("=", 1)[0].strip()] = raw
    return out


@pytest.fixture
def env(monkeypatch):
    """Redis falso + Google/Instagram falsos (nada sale a la red ni a la BD)."""
    from app.api import oauth

    redis = _FakeRedis()
    monkeypatch.setattr(oauth, "get_redis", lambda: redis)

    calls: list[str] = []

    async def fake_exchange_code(code: str):
        calls.append(f"google:exchange:{code}")
        raise RuntimeError("no debería llegar aquí en un flujo rechazado")

    async def fake_ig_exchange_code(code: str):
        calls.append(f"ig:exchange:{code}")
        raise RuntimeError("no debería llegar aquí en un flujo rechazado")

    monkeypatch.setattr(oauth, "exchange_code", fake_exchange_code)
    monkeypatch.setattr(oauth.ig_oauth, "exchange_code", fake_ig_exchange_code)
    return redis, calls


# ---------------------------------------------------------------------------
# El arranque deja la cookie
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_google_start_deja_el_state_en_una_cookie_httponly(env, monkeypatch):
    from fastapi import Response

    from app.api import admin, oauth

    redis, _calls = env
    monkeypatch.setattr("app.core.redis.get_redis", lambda: redis)

    async def fake_build_auth_url(provider: str, state: str) -> str:
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    monkeypatch.setattr(
        "app.services.google_oauth.build_auth_url", fake_build_auth_url
    )

    response = Response()
    out = await admin.google_oauth_start(
        admin.GoogleOAuthStartIn(provider="google_calendar"),
        response=response,
        user=None,
    )
    assert out.auth_url.startswith("https://accounts.google.com/")

    state = next(k.removeprefix(oauth._STATE_PREFIX) for k in redis.store)
    cookies = _set_cookies(response)
    assert oauth._STATE_COOKIE in cookies, (
        "el arranque no deja cookie: el state no está atado al navegador"
    )
    raw = cookies[oauth._STATE_COOKIE]
    assert f"{oauth._STATE_COOKIE}={state}" in raw
    assert "HttpOnly" in raw
    assert "SameSite=lax" in raw.replace("samesite", "SameSite")
    assert "Max-Age=" in raw


@pytest.mark.asyncio
async def test_instagram_start_deja_el_state_en_una_cookie_httponly(env, monkeypatch):
    from fastapi import Response

    from app.api import admin, oauth

    redis, _calls = env
    monkeypatch.setattr("app.core.redis.get_redis", lambda: redis)

    async def fake_build_auth_url(state: str) -> str:
        return f"https://www.instagram.com/oauth/authorize?state={state}"

    monkeypatch.setattr(
        "app.services.instagram_oauth.build_auth_url", fake_build_auth_url
    )

    response = Response()
    out = await admin.instagram_oauth_start(response=response, user=None)
    assert out.auth_url.startswith("https://www.instagram.com/")

    state = next(k.removeprefix(oauth._STATE_PREFIX) for k in redis.store)
    raw = _set_cookies(response).get(oauth._STATE_COOKIE)
    assert raw is not None, "el arranque de Instagram no deja cookie de state"
    assert f"{oauth._STATE_COOKIE}={state}" in raw
    assert "HttpOnly" in raw


# ---------------------------------------------------------------------------
# El callback exige la cookie
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_google_callback_sin_cookie_no_guarda_tokens(env):
    """El caso del ataque: state válido en Redis, pero otro navegador."""
    from app.api import oauth

    redis, calls = env
    state = "state-suelto"
    await redis.setex(oauth._STATE_PREFIX + state, 600, "google_calendar")

    resp = await oauth.google_callback(
        _request("/api/v1/oauth/google/callback"), code="codigo", state=state, error=None
    )

    assert resp.status_code == 200
    assert b"Error" in resp.body
    assert not any(c.startswith("google:exchange") for c in calls), (
        "se llegó a canjear el code desde otro navegador"
    )
    # El state NO se consume: el flujo legítimo del navegador que lo pidió
    # todavía tiene que poder terminar.
    assert oauth._STATE_PREFIX + state in redis.store


@pytest.mark.asyncio
async def test_google_callback_con_cookie_de_otro_flujo_no_guarda_tokens(env):
    from app.api import oauth

    redis, calls = env
    state = "state-suelto"
    await redis.setex(oauth._STATE_PREFIX + state, 600, "google_calendar")

    resp = await oauth.google_callback(
        _request(
            "/api/v1/oauth/google/callback",
            cookies={oauth._STATE_COOKIE: "otro-state-distinto"},
        ),
        code="codigo",
        state=state,
        error=None,
    )

    assert b"Error" in resp.body
    assert not any(c.startswith("google:exchange") for c in calls)


@pytest.mark.asyncio
async def test_google_callback_con_su_propia_cookie_continua(env):
    """El flujo legítimo sigue funcionando: mismo navegador → se canjea el code."""
    from app.api import oauth

    redis, calls = env
    state = "state-legitimo"
    await redis.setex(oauth._STATE_PREFIX + state, 600, "google_calendar")

    resp = await oauth.google_callback(
        _request(
            "/api/v1/oauth/google/callback", cookies={oauth._STATE_COOKIE: state}
        ),
        code="codigo",
        state=state,
        error=None,
    )

    assert "google:exchange:codigo" in calls, "no se canjeó el code del flujo legítimo"
    # exchange_code revienta a propósito → sale por el error de conexión.
    assert b"Error" in resp.body
    # Y la cookie se borra al cerrar el flujo (no queda state reutilizable).
    raw = _set_cookies(resp).get(oauth._STATE_COOKIE)
    assert raw is not None
    assert "Max-Age=0" in raw or "expires=Thu, 01 Jan 1970" in raw.lower()


@pytest.mark.asyncio
async def test_instagram_callback_sin_cookie_no_guarda_tokens(env):
    from app.api import oauth

    redis, calls = env
    state = "state-suelto-ig"
    await redis.setex(oauth._STATE_PREFIX + state, 600, "instagram_business")

    resp = await oauth.instagram_callback(
        _request("/api/v1/oauth/instagram/callback"), code="codigo", state=state, error=None
    )

    assert b"Error" in resp.body
    assert not any(c.startswith("ig:exchange") for c in calls)
    assert oauth._STATE_PREFIX + state in redis.store


@pytest.mark.asyncio
async def test_instagram_callback_con_su_propia_cookie_continua(env):
    from app.api import oauth

    redis, calls = env
    state = "state-legitimo-ig"
    await redis.setex(oauth._STATE_PREFIX + state, 600, "instagram_business")

    resp = await oauth.instagram_callback(
        _request(
            "/api/v1/oauth/instagram/callback", cookies={oauth._STATE_COOKIE: state}
        ),
        code="codigo",
        state=state,
        error=None,
    )

    assert "ig:exchange:codigo" in calls
    raw = _set_cookies(resp).get(oauth._STATE_COOKIE)
    assert raw is not None
    assert "Max-Age=0" in raw or "expires=Thu, 01 Jan 1970" in raw.lower()
