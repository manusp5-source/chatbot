"""Proveedor Gemini: capa compatible con OpenAI + normalización del id.

Gemini no lleva cliente nativo a propósito. Google publica una capa compatible
con la API de OpenAI (https://ai.google.dev/gemini-api/docs/openai) que soporta
chat completions, function calling, streaming y structured outputs — todo lo que
usa este chatbot. Reusar el `OpenAIProvider`, que es el que está en producción y
el que tiene los tests, es menos código y menos superficie de fallo que traducir
a `contents`/`parts` a mano.

Lo que sí hay que probar es lo poco que Gemini hace distinto: la base_url por
defecto y el prefijo `models/` de sus ids.
"""
import pytest

from app.providers.llm.base import LLMMessage, LLMToolSchema
from app.providers.llm.families import GEMINI_BASE_URL, detect_family
from app.providers.llm.gemini_client import GeminiProvider, normalize_gemini_model


class _FakeCompletions:
    def __init__(self, sink):
        self.sink = sink

    async def create(self, **params):
        self.sink.update(params)

        class _Msg:
            content = "ok"
            tool_calls = None

        class _Choice:
            message = _Msg()
            finish_reason = "stop"

        class _Resp:
            choices = [_Choice()]
            usage = None

        return _Resp()


class _FakeClient:
    def __init__(self, sink):
        self.chat = type("C", (), {"completions": _FakeCompletions(sink)})()


def _provider(monkeypatch, sink, **kw):
    p = GeminiProvider(explicit_api_key="k", **kw)
    monkeypatch.setattr(p, "_client", lambda *a, **k: _FakeClient(sink))
    return p


# --------------------- Normalización del id de modelo ---------------------


def test_se_quita_el_prefijo_models():
    """El listado de Google devuelve `models/gemini-…` (herencia de su API
    nativa, donde el nombre del recurso es la ruta). Si ese id llega tal cual a
    la ficha del agente, la tabla de precios no lo encuentra y el gasto del
    modelo cuenta 0 contra el tope."""
    assert normalize_gemini_model("models/gemini-2.5-flash") == "gemini-2.5-flash"
    assert normalize_gemini_model("models/gemini-3.1-pro-preview") == "gemini-3.1-pro-preview"


def test_un_id_ya_limpio_no_se_toca():
    assert normalize_gemini_model("gemini-2.5-flash") == "gemini-2.5-flash"
    assert normalize_gemini_model(None) is None
    assert normalize_gemini_model("") == ""


@pytest.mark.asyncio
async def test_se_llama_con_el_id_normalizado(monkeypatch):
    """La normalización va en el único sitio por el que pasan todas las
    llamadas, para que el id que se usa, se apunta y se tarifa sea el mismo."""
    sink: dict = {}
    p = _provider(monkeypatch, sink)
    await p.complete(
        [LLMMessage(role="user", content="hola")], model="models/gemini-2.5-flash"
    )
    assert sink["model"] == "gemini-2.5-flash"


# --------------------------- Configuración ---------------------------


def test_el_host_de_google_se_detecta_como_familia_gemini():
    assert detect_family(GEMINI_BASE_URL) == "gemini"
    assert detect_family("https://generativelanguage.googleapis.com/v1beta/openai/") == "gemini"


@pytest.mark.asyncio
async def test_la_base_url_por_defecto_es_la_capa_openai_de_google():
    p = GeminiProvider(explicit_api_key="k")
    _key, base_url, _model = await p._resolve_config()
    assert base_url == GEMINI_BASE_URL


@pytest.mark.asyncio
async def test_is_configured_solo_pide_la_clave():
    """La URL es fija y el modelo lo pone el agente: con la clave basta. Si
    pidiera además base_url y modelo como una pasarela genérica, un Gemini bien
    configurado nunca llegaría a usarse como respaldo."""
    assert await GeminiProvider(explicit_api_key="k").is_configured() is True
    assert (
        await GeminiProvider(
            explicit_api_key=None, api_key_credential="__no_existe__"
        ).is_configured()
        is False
    )


# ------------------- Herramientas por la capa compatible -------------------


@pytest.mark.asyncio
async def test_las_tools_van_en_el_formato_de_openai(monkeypatch):
    """La capa compatible de Google acepta el formato de function calling de
    OpenAI, así que el orquestador no necesita nada especial."""
    sink: dict = {}
    p = _provider(monkeypatch, sink)
    await p.complete(
        [LLMMessage(role="user", content="hola")],
        tools=[
            LLMToolSchema(
                name="consultar_kb",
                description="Busca",
                parameters={"type": "object", "properties": {}},
            )
        ],
        model="gemini-2.5-flash",
    )
    assert sink["tools"][0]["type"] == "function"
    assert sink["tools"][0]["function"]["name"] == "consultar_kb"


@pytest.mark.asyncio
async def test_gemini_si_acepta_temperature(monkeypatch):
    sink: dict = {}
    p = _provider(monkeypatch, sink)
    await p.complete(
        [LLMMessage(role="user", content="hola")], model="gemini-2.5-flash", temperature=0.3
    )
    assert sink["temperature"] == 0.3
    # Y `max_tokens`, no `max_completion_tokens` (que es cosa de los reasoning
    # de OpenAI): un modelo llamado "gemini-…" no puede caer en esa rama.
    await p.complete(
        [LLMMessage(role="user", content="hola")], model="gemini-2.5-flash", max_tokens=256
    )
    assert sink["max_tokens"] == 256
    assert "max_completion_tokens" not in sink
