"""El `state` del login con Google tiene que estar atado AL NAVEGADOR.

El fallo que cubre este test: el callback solo comprobaba que el `state`
existiera en Redis. Como Redis es global, valía CUALQUIER state vivo, lo hubiera
pedido quien lo hubiera pedido. Un atacante arranca el flujo con su cuenta,
guarda el enlace del callback (`?code=...&state=...`) y se lo hace abrir a la
víctima: la víctima acaba con sesión abierta en la cuenta DEL ATACANTE
(session fixation) y todo lo que escriba ahí lo lee él.

El arreglo: el `state` viaja también en una cookie HttpOnly de vida corta que
solo tiene el navegador que inició el flujo, y el callback exige que coincida.

Sin BD: se corta antes de tocar Postgres en todos los casos que se prueban.
"""
from __future__ import annotations

import pytest
from starlette.requests import Request


# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


def _request(cookies: dict[str, str] | None = None) -> Request:
    headers = []
    if cookies:
        raw = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.append((b"cookie", raw.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/auth/google/callback",
            "headers": headers,
            "query_string": b"",
            "client": ("203.0.113.9", 51234),
            "scheme": "https",
            "server": ("api.test", 443),
        }
    )


@pytest.fixture
def env(monkeypatch):
    """Redis falso + rate-limit desactivado + Google falso."""
    from app.api import auth

    redis = _FakeRedis()
    monkeypatch.setattr(auth, "get_redis", lambda: redis)
    # El decorador de slowapi iría a Redis de verdad; lo desactivamos.
    monkeypatch.setattr(auth.limiter, "enabled", False)

    calls: list[str] = []

    async def fake_build_login_url(state: str) -> str:
        calls.append(state)
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    async def fake_fetch_identity(code: str):
        calls.append(f"exchange:{code}")
        raise RuntimeError("no debería llegar aquí en un flujo rechazado")

    monkeypatch.setattr(auth, "build_login_url", fake_build_login_url)
    monkeypatch.setattr(auth, "fetch_identity", fake_fetch_identity)
    return redis, calls


def _set_cookies(response) -> dict[str, str]:
    """{nombre: cabecera Set-Cookie completa} de la respuesta."""
    out = {}
    for raw in response.headers.getlist("set-cookie"):
        out[raw.split("=", 1)[0].strip()] = raw
    return out


def _fragment(response) -> str:
    return response.headers["location"].split("#", 1)[-1]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_deja_el_state_en_una_cookie_httponly(env):
    from app.api import auth

    redis, calls = env
    resp = await auth.google_login_start(_request())

    state = calls[0]
    assert redis.store, "el state debería seguir guardándose en Redis"

    cookies = _set_cookies(resp)
    assert auth._GLOGIN_STATE_COOKIE in cookies, (
        "el flujo no deja ninguna cookie: el state no está atado al navegador"
    )
    raw = cookies[auth._GLOGIN_STATE_COOKIE]
    assert f"{auth._GLOGIN_STATE_COOKIE}={state}" in raw
    assert "HttpOnly" in raw
    assert "SameSite=lax" in raw.replace("samesite", "SameSite")
    assert "Max-Age=" in raw


@pytest.mark.asyncio
async def test_callback_sin_cookie_no_inicia_sesion(env):
    """El caso del ataque: state válido en Redis, pero otro navegador."""
    from app.api import auth

    redis, calls = env
    state = "state-del-atacante"
    await redis.set(auth._GLOGIN_STATE_PREFIX + state, "1")

    resp = await auth.google_login_callback(
        _request(), code="codigo-del-atacante", state=state, db=None
    )

    assert _fragment(resp) == "google_error=google_state"
    assert not any(c.startswith("exchange:") for c in calls), (
        "se llegó a canjear el code de otro navegador"
    )


@pytest.mark.asyncio
async def test_callback_con_cookie_de_otro_flujo_no_inicia_sesion(env):
    from app.api import auth

    redis, calls = env
    state = "state-del-atacante"
    await redis.set(auth._GLOGIN_STATE_PREFIX + state, "1")

    resp = await auth.google_login_callback(
        _request(cookies={auth._GLOGIN_STATE_COOKIE: "otro-state-distinto"}),
        code="codigo",
        state=state,
        db=None,
    )

    assert _fragment(resp) == "google_error=google_state"
    assert not any(c.startswith("exchange:") for c in calls)


@pytest.mark.asyncio
async def test_callback_con_su_propia_cookie_continua(env):
    """El flujo legítimo sigue funcionando: mismo navegador → se canjea el code."""
    from app.api import auth

    redis, calls = env
    state = "state-legitimo"
    await redis.set(auth._GLOGIN_STATE_PREFIX + state, "1")

    resp = await auth.google_login_callback(
        _request(cookies={auth._GLOGIN_STATE_COOKIE: state}),
        code="codigo",
        state=state,
        db=None,
    )

    assert "exchange:codigo" in calls, "no se canjeó el code del flujo legítimo"
    # fetch_identity revienta a propósito → error de intercambio, no de state.
    assert _fragment(resp) == "google_error=google_exchange"


@pytest.mark.asyncio
async def test_el_callback_borra_la_cookie_al_terminar(env):
    from app.api import auth

    redis, _calls = env
    state = "state-legitimo"
    await redis.set(auth._GLOGIN_STATE_PREFIX + state, "1")

    resp = await auth.google_login_callback(
        _request(cookies={auth._GLOGIN_STATE_COOKIE: state}),
        code="codigo",
        state=state,
        db=None,
    )

    raw = _set_cookies(resp).get(auth._GLOGIN_STATE_COOKIE)
    assert raw is not None, "la cookie de state no se borra al cerrar el flujo"
    assert "Max-Age=0" in raw or 'expires=Thu, 01 Jan 1970' in raw.lower()
