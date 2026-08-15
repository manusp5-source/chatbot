"""Parámetros que se mandan al modelo y qué pasa sin credenciales.

Dos fallos:
  - con el modelo POR DEFECTO (gpt-5.4-mini, un reasoning model) no se mandaba
    ni `temperature` ni `max_tokens`. La temperature no se puede mandar, de
    acuerdo, pero el tope de salida SÍ: allí se llama `max_completion_tokens`.
    Así que el campo "Max tokens" del formulario de Agentes no hacía nada.
  - sin clave, el cliente devolvía como RESPUESTA el texto "(LLM no configurado.
    Configura las claves en el panel admin.)" y ese mensaje interno del panel
    acababa enviándose al cliente final del negocio por WhatsApp.
"""
import pytest

from app.providers.llm.base import LLMMessage, LLMNotConfiguredError
from app.providers.llm.openai_client import OpenAIProvider, is_reasoning_model

MSGS = [LLMMessage(role="user", content="hola")]


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


def _provider_with(monkeypatch, sink, api_key="k"):
    from app.providers.llm import openai_client as oc

    async def fake_get_credential(_key):
        return api_key

    monkeypatch.setattr(oc, "get_credential", fake_get_credential)
    p = OpenAIProvider(api_key_credential="openai_api_key", label="openai")
    monkeypatch.setattr(p, "_client", lambda *a, **k: _FakeClient(sink))
    return p


def test_deteccion_de_reasoning_model():
    assert is_reasoning_model("gpt-5.4-mini")
    assert is_reasoning_model("o3-mini")
    assert not is_reasoning_model("gpt-4o")
    assert not is_reasoning_model("deepseek/deepseek-v4-flash")


@pytest.mark.asyncio
async def test_reasoning_model_usa_max_completion_tokens_y_omite_temperature(monkeypatch):
    sink: dict = {}
    p = _provider_with(monkeypatch, sink)
    await p.complete(MSGS, model="gpt-5.4-mini", temperature=0.2, max_tokens=512)

    assert sink["model"] == "gpt-5.4-mini"
    # El tope SÍ llega, con el nombre que acepta el modelo.
    assert sink["max_completion_tokens"] == 512
    assert "max_tokens" not in sink
    # La temperature no se puede mandar a un reasoning model (da 400).
    assert "temperature" not in sink


@pytest.mark.asyncio
async def test_modelo_clasico_usa_max_tokens_y_temperature(monkeypatch):
    sink: dict = {}
    p = _provider_with(monkeypatch, sink)
    await p.complete(MSGS, model="gpt-4o", temperature=0.2, max_tokens=512)

    assert sink["max_tokens"] == 512
    assert sink["temperature"] == 0.2
    assert "max_completion_tokens" not in sink


@pytest.mark.asyncio
async def test_sin_clave_lanza_excepcion_en_vez_de_contestar(monkeypatch):
    """Lo que llegaba al cliente final del negocio el día 1 de la instalación."""
    sink: dict = {}
    p = _provider_with(monkeypatch, sink, api_key="")

    with pytest.raises(LLMNotConfiguredError):
        await p.complete(MSGS, model="gpt-5.4-mini")
    # Y no se ha llegado a llamar al modelo.
    assert sink == {}
