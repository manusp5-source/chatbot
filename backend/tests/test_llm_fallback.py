"""Tests del failover de proveedor LLM (FallbackLLMProvider).

Usan dobles en memoria (no tocan OpenAI, ni DB, ni Redis): el log de failover
es best-effort y se traga sus propios errores, así que no hace falta infra.
"""
import pytest

from app.providers.llm.base import LLMCompletion, LLMMessage, LLMNotConfiguredError
from app.providers.llm.openai_client import FallbackLLMProvider

MSGS = [LLMMessage(role="user", content="hola")]


class FakePrimary:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = 0

    async def complete(self, messages, tools=None, model=None, temperature=None,
                       max_tokens=None, source="agent"):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


class FakeSecondary:
    def __init__(self, configured=True, result=None, exc=None):
        self._configured = configured
        self.result = result
        self.exc = exc
        self.calls = 0
        self.last_model = "unset"

    async def is_configured(self):
        return self._configured

    async def complete(self, messages, tools=None, model=None, temperature=None,
                       max_tokens=None, source="agent"):
        self.calls += 1
        self.last_model = model
        if self.exc:
            raise self.exc
        return self.result


def _ok(text):
    return LLMCompletion(content=text, tool_calls=[], finish_reason="stop")


def _no_key_exc():
    """"Sin clave" es una EXCEPCIÓN, no una respuesta con texto de aviso."""
    return LLMNotConfiguredError("sin credenciales")


@pytest.mark.asyncio
async def test_primary_ok_no_failover():
    primary = FakePrimary(result=_ok("ok"))
    secondary = FakeSecondary(configured=True, result=_ok("sec"))
    out = await FallbackLLMProvider(primary, secondary).complete(MSGS)
    assert out.content == "ok"
    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_failover_on_exception_uses_secondary_own_model():
    primary = FakePrimary(exc=RuntimeError("boom"))
    secondary = FakeSecondary(configured=True, result=_ok("sec"))
    out = await FallbackLLMProvider(primary, secondary).complete(MSGS, model="gpt-5.4-mini")
    assert out.content == "sec"
    assert secondary.calls == 1
    # El secundario debe usar SU modelo (model=None), no el del agente.
    assert secondary.last_model is None


@pytest.mark.asyncio
async def test_exception_without_secondary_propagates():
    primary = FakePrimary(exc=RuntimeError("boom"))
    secondary = FakeSecondary(configured=False)
    with pytest.raises(RuntimeError):
        await FallbackLLMProvider(primary, secondary).complete(MSGS)
    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_primary_no_key_uses_secondary():
    primary = FakePrimary(exc=_no_key_exc())
    secondary = FakeSecondary(configured=True, result=_ok("sec"))
    out = await FallbackLLMProvider(primary, secondary).complete(MSGS)
    assert out.content == "sec"
    assert secondary.calls == 1


@pytest.mark.asyncio
async def test_primary_no_key_without_secondary_propagates():
    """Sin primario NI secundario NO se devuelve texto: se propaga el fallo.

    Antes esto devolvía como respuesta "(LLM no configurado. Configura las
    claves en el panel admin.)" y ese mensaje interno acababa enviándose al
    cliente final por WhatsApp. Ahora propaga y `conversation.py` deriva a una
    persona.
    """
    primary = FakePrimary(exc=_no_key_exc())
    secondary = FakeSecondary(configured=False)
    with pytest.raises(LLMNotConfiguredError):
        await FallbackLLMProvider(primary, secondary).complete(MSGS)
    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_both_fail_propagates():
    primary = FakePrimary(exc=RuntimeError("boom1"))
    secondary = FakeSecondary(configured=True, exc=RuntimeError("boom2"))
    with pytest.raises(RuntimeError):
        await FallbackLLMProvider(primary, secondary).complete(MSGS)
    assert secondary.calls == 1


@pytest.mark.asyncio
async def test_secondary_defaults_openrouter_and_model(monkeypatch):
    """Con base_url y modelo vacíos, el secundario usa OpenRouter + deepseek;
    basta con la API key para quedar configurado."""
    from app.providers.llm import openai_client as oc

    creds = {
        "llm_fallback_api_key": "k",
        "llm_fallback_base_url": "",  # vacío → default OpenRouter
        "llm_fallback_model": "",     # vacío → default deepseek
    }

    async def fake_get_credential(key):
        return creds.get(key, "")

    monkeypatch.setattr(oc, "get_credential", fake_get_credential)
    sec = oc.OpenAIProvider(
        api_key_credential="llm_fallback_api_key",
        base_url_credential="llm_fallback_base_url",
        model_credential="llm_fallback_model",
        default_base_url=oc.OPENROUTER_BASE_URL,
        default_model=oc.DEFAULT_FALLBACK_MODEL,
        label="fallback",
    )
    api_key, base_url, model = await sec._resolve_config()
    assert base_url == oc.OPENROUTER_BASE_URL
    assert model == oc.DEFAULT_FALLBACK_MODEL
    assert await sec.is_configured() is True

    creds["llm_fallback_api_key"] = ""  # sin clave → no configurado
    assert await sec.is_configured() is False
