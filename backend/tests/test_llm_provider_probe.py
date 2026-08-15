"""Botón «Probar» del panel: clasificar bien el error y listar los modelos.

El caso que motiva medio módulo es Google. Casi todas las APIs contestan 401 o
403 a una clave inválida; Google contesta **400** con "Please pass a valid API
key" (comprobado con una llamada real el 11-ago-2026 contra
generativelanguage.googleapis.com, tanto en /models como en /chat/completions).
Un comprobador que solo mire 401/403 le enseña al usuario un «HTTP 400» opaco y
no hay forma de saber si se equivocó de clave, de URL o de modelo. Es un fallo
fácil de reintroducir y es lo que estos tests impiden que vuelva a pasar.

Sin red: se sustituye httpx.AsyncClient por un doble con respuestas guionizadas.
"""
import json

import pytest

from app.providers.llm import probe as probe_mod
from app.providers.llm.probe import _is_auth_error, probe_provider

GOOGLE_400_BODY = json.dumps(
    {"error": {"code": 400, "message": "Please pass a valid API key", "status": "INVALID_ARGUMENT"}}
)


# --------------------------- Doble de httpx ---------------------------


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Devuelve respuestas guionizadas y apunta lo que se ha pedido."""

    def __init__(self, get=None, post=None, calls=None, **kw):
        self._get = get or []
        self._post = post
        self.calls = calls if calls is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        self.calls.append(("GET", url, headers or {}, params or {}))
        if isinstance(self._get, list):
            return self._get[min(len(self.calls) - 1, len(self._get) - 1)]
        return self._get

    async def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, headers or {}, json or {}))
        return self._post


def _patch_httpx(monkeypatch, **kw):
    calls: list = []
    monkeypatch.setattr(
        probe_mod.httpx, "AsyncClient", lambda **_: _FakeAsyncClient(calls=calls, **kw)
    )
    return calls


# --------------------- Clasificación del error ---------------------


def test_401_y_403_son_clave_invalida():
    assert _is_auth_error(401, "")
    assert _is_auth_error(403, "")


def test_el_400_de_google_se_reconoce_como_clave_invalida():
    """EL caso. Google no usa 401."""
    assert _is_auth_error(400, GOOGLE_400_BODY)


def test_un_400_que_no_habla_de_la_clave_no_es_clave_invalida():
    """Un 400 por petición mal formada no debe reportarse como clave mala:
    mandaría al usuario a cambiar una clave que estaba bien."""
    assert not _is_auth_error(400, '{"error":{"message":"model not found"}}')
    assert not _is_auth_error(400, "max_tokens must be positive")


def test_un_500_no_es_clave_invalida():
    assert not _is_auth_error(500, "internal error")


# --------------------------- Gemini ---------------------------


@pytest.mark.asyncio
async def test_gemini_con_clave_invalida_da_mensaje_util_no_un_http_400(monkeypatch):
    _patch_httpx(monkeypatch, get=_Resp(400, text=GOOGLE_400_BODY))
    r = await probe_provider(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai", api_key="mala"
    )
    assert r.ok is False
    assert "clave" in r.message.lower()
    assert "400" not in r.message


@pytest.mark.asyncio
async def test_gemini_lista_modelos_sin_el_prefijo_models(monkeypatch):
    """Google devuelve `models/gemini-…`. Si ese id llega tal cual a la ficha
    del agente, la tabla de precios no lo encuentra y el gasto cuenta 0."""
    _patch_httpx(
        monkeypatch,
        get=_Resp(
            200,
            payload={
                "data": [
                    {"id": "models/gemini-2.5-flash"},
                    {"id": "models/gemini-3.1-pro-preview"},
                ]
            },
        ),
    )
    r = await probe_provider(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai", api_key="k"
    )
    assert r.ok is True
    assert r.models == ["gemini-2.5-flash", "gemini-3.1-pro-preview"]


@pytest.mark.asyncio
async def test_gemini_usa_bearer(monkeypatch):
    calls = _patch_httpx(monkeypatch, get=_Resp(200, payload={"data": []}))
    await probe_provider(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai", api_key="k"
    )
    _method, url, headers, _params = calls[0]
    assert url.endswith("/v1beta/openai/models")
    assert headers["Authorization"] == "Bearer k"


# --------------------------- Anthropic ---------------------------


@pytest.mark.asyncio
async def test_anthropic_usa_x_api_key_y_version_no_bearer(monkeypatch):
    """Con `Authorization: Bearer` Anthropic contesta 401 aunque la clave sea
    buena, y sin `anthropic-version` contesta 400."""
    calls = _patch_httpx(monkeypatch, get=_Resp(200, payload={"data": [], "has_more": False}))
    await probe_provider(base_url="https://api.anthropic.com/v1", api_key="sk-ant-xxx")
    _method, url, headers, _params = calls[0]
    assert url.endswith("/v1/models")
    assert headers["x-api-key"] == "sk-ant-xxx"
    assert headers["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_anthropic_clave_invalida_da_401(monkeypatch):
    _patch_httpx(
        monkeypatch,
        get=_Resp(401, text='{"error":{"type":"authentication_error"}}'),
    )
    r = await probe_provider(base_url="https://api.anthropic.com/v1", api_key="mala")
    assert r.ok is False
    assert "clave" in r.message.lower()


@pytest.mark.asyncio
async def test_anthropic_pagina_el_listado_de_modelos(monkeypatch):
    """El listado devuelve páginas: sin paginar, el desplegable enseñaría un
    recorte arbitrario del catálogo."""
    _patch_httpx(
        monkeypatch,
        get=[
            _Resp(
                200,
                payload={
                    "data": [{"id": "claude-opus-5"}, {"id": "claude-sonnet-5"}],
                    "has_more": True,
                    "last_id": "claude-sonnet-5",
                },
            ),
            _Resp(200, payload={"data": [{"id": "claude-haiku-4-5"}], "has_more": False}),
        ],
    )
    r = await probe_provider(base_url="https://api.anthropic.com/v1", api_key="k")
    assert r.ok is True
    assert r.models == ["claude-haiku-4-5", "claude-opus-5", "claude-sonnet-5"]


# ------------------- OpenAI y compatibles (no romper) -------------------


@pytest.mark.asyncio
async def test_openai_sigue_funcionando_igual(monkeypatch):
    calls = _patch_httpx(
        monkeypatch, get=_Resp(200, payload={"data": [{"id": "gpt-5.4-mini"}]})
    )
    r = await probe_provider(base_url=None, api_key="sk-x")
    assert r.ok is True
    assert r.models == ["gpt-5.4-mini"]
    _method, url, headers, _params = calls[0]
    assert url == "https://api.openai.com/v1/models"
    assert headers["Authorization"] == "Bearer sk-x"


@pytest.mark.asyncio
async def test_pasarela_sin_endpoint_de_modelos_valida_la_clave_con_un_ping(monkeypatch):
    """Z.AI Coding Plan no expone /models: la clave se valida con una chat
    completion de modelo inexistente. Comportamiento que ya existía."""
    _patch_httpx(
        monkeypatch,
        get=_Resp(404, text="not found"),
        post=_Resp(400, text='{"error":{"message":"unknown model"}}'),
    )
    r = await probe_provider(base_url="https://api.z.ai/api/coding/paas/v4", api_key="k")
    assert r.ok is True
    assert r.models == []
    assert "no expone la lista de modelos" in r.message


@pytest.mark.asyncio
async def test_sin_clave_no_se_llama_a_nadie(monkeypatch):
    calls = _patch_httpx(monkeypatch, get=_Resp(200, payload={"data": []}))
    r = await probe_provider(base_url=None, api_key="")
    assert r.ok is False
    assert calls == []


@pytest.mark.asyncio
async def test_un_fallo_de_red_no_revienta_el_panel(monkeypatch):
    def _boom(**_kw):
        raise RuntimeError("dns")

    monkeypatch.setattr(probe_mod.httpx, "AsyncClient", _boom)
    r = await probe_provider(base_url="https://api.anthropic.com/v1", api_key="k")
    assert r.ok is False
    assert "No se pudo conectar" in r.message


# ------------- El endpoint del panel usa ESTE comprobador -------------
#
# El botón "Probar" y el desplegable de modelos de la ficha del agente cuelgan
# los dos de POST /admin/llm-providers/{id}/test. Ese endpoint tenía su propia
# copia del protocolo de OpenAI (`GET /models` con `Authorization: Bearer` para
# todo el mundo), así que un proveedor de Anthropic contestaba 401 y el panel
# decía "El proveedor rechazó la clave API" con una clave buena. Este test fija
# que el endpoint delega en probe_provider, que es quien sabe de familias.


class _FakeProvider:
    def __init__(self, base_url, api_key):
        self.base_url = base_url
        self.api_key = api_key


class _FakeResult:
    def __init__(self, provider):
        self._p = provider

    def scalar_one_or_none(self):
        return self._p


class _FakeDB:
    def __init__(self, provider):
        self._p = provider

    async def execute(self, *_a, **_kw):
        return _FakeResult(self._p)


@pytest.mark.asyncio
async def test_endpoint_del_panel_prueba_anthropic_con_su_protocolo(monkeypatch):
    from app.api.admin import test_llm_provider

    calls = _patch_httpx(
        monkeypatch,
        get=_Resp(200, payload={"data": [{"id": "claude-opus-4-8"}], "has_more": False}),
    )
    db = _FakeDB(_FakeProvider("https://api.anthropic.com/v1", "sk-ant-xxx"))

    out = await test_llm_provider(provider_id="ignorado", db=db, _=None)

    assert out.ok is True
    assert out.models == ["claude-opus-4-8"]
    # La prueba de verdad: se habló el protocolo de Anthropic, no el de OpenAI.
    _, url, headers, _params = calls[0]
    assert url == "https://api.anthropic.com/v1/models"
    assert headers["x-api-key"] == "sk-ant-xxx"
    assert "Authorization" not in headers
