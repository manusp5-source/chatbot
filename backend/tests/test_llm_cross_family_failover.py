"""Failover ENTRE FAMILIAS distintas: Claude ↔ GPT ↔ Gemini.

El respaldo existía para pasarelas compatibles entre sí: primario y secundario
eran el mismo cliente con otra URL, así que "el mismo prompt y las mismas tools"
era gratis. Con Anthropic ya no: si Claude cae y el respaldo es GPT, hay que
volver a traducir el MISMO historial —system aparte, tool_use como bloques— al
formato del otro proveedor, a mitad de una conversación que puede ir ya por la
tercera llamada a herramienta.

Estos tests fijan que el failover cruzado funcione en los dos sentidos y que la
elección de cliente se haga por familia al resolver el proveedor del agente.
"""
import pytest

from app.providers.llm.anthropic_client import AnthropicProvider
from app.providers.llm.base import (
    LLMCompletion,
    LLMMessage,
    LLMNotConfiguredError,
    LLMToolCall,
    LLMToolSchema,
)
from app.providers.llm.gemini_client import GeminiProvider
from app.providers.llm.openai_client import (
    FallbackLLMProvider,
    OpenAIProvider,
    _primary_from_row,
)

# Historial realista a mitad de un bucle de herramientas: es el caso en el que
# el failover cruzado se puede romper de verdad.
MSGS = [
    LLMMessage(role="system", content="Eres el bot de la tienda."),
    LLMMessage(role="user", content="¿cuánto cuesta el curso?"),
    LLMMessage(
        role="assistant",
        content=None,
        tool_calls=[LLMToolCall(id="toolu_1", name="consultar_kb", arguments={"q": "precio"})],
    ),
    LLMMessage(role="tool", tool_call_id="toolu_1", content='{"precio":"66 €"}', name="consultar_kb"),
]
TOOLS = [
    LLMToolSchema(
        name="consultar_kb", description="Busca", parameters={"type": "object", "properties": {}}
    )
]


def _ok(text):
    return LLMCompletion(content=text, tool_calls=[], finish_reason="stop", usage={})


class _Row:
    """Fila de llm_providers mínima (sin BD)."""

    def __init__(self, name, base_url, api_key="k", accepts_temperature=True):
        self.name = name
        self.base_url = base_url
        self.api_key = api_key
        self.accepts_temperature = accepts_temperature


class _Spy:
    """Proveedor doble que apunta con qué se le llamó."""

    def __init__(self, result=None, exc=None, configured=True):
        self.result = result
        self.exc = exc
        self._configured = configured
        self.calls = 0
        self.last_messages = None
        self.last_tools = None
        self.last_model = "unset"

    async def is_configured(self):
        return self._configured

    async def complete(
        self, messages, tools=None, model=None, temperature=None, max_tokens=None, source="agent"
    ):
        self.calls += 1
        self.last_messages = messages
        self.last_tools = tools
        self.last_model = model
        if self.exc:
            raise self.exc
        return self.result


# --------------- Elección de cliente según la familia ---------------


def test_una_fila_de_anthropic_construye_el_cliente_de_anthropic():
    p = _primary_from_row(_Row("Claude", "https://api.anthropic.com/v1"))
    assert isinstance(p, AnthropicProvider)


def test_una_fila_de_gemini_construye_el_cliente_de_gemini():
    p = _primary_from_row(
        _Row("Gemini", "https://generativelanguage.googleapis.com/v1beta/openai")
    )
    assert isinstance(p, GeminiProvider)


def test_una_pasarela_desconocida_sigue_usando_el_cliente_de_openai():
    """Lo que está en producción hoy no puede cambiar de clase."""
    assert isinstance(_primary_from_row(_Row("OpenRouter", "https://openrouter.ai/api/v1")), OpenAIProvider)
    assert not isinstance(
        _primary_from_row(_Row("OpenRouter", "https://openrouter.ai/api/v1")), AnthropicProvider
    )


def test_el_proveedor_openai_sembrado_sin_url_ni_clave_no_cambia():
    p = _primary_from_row(_Row("OpenAI", None, api_key=None))
    assert isinstance(p, OpenAIProvider)
    assert p.api_key_credential == "openai_api_key"


def test_el_rol_de_respaldo_fija_el_modelo_en_cualquier_familia():
    """Los ids del primario no existen en el respaldo: hay que forzar el suyo."""
    a = _primary_from_row(_Row("Claude", "https://api.anthropic.com/v1"), forced_model="claude-opus-5")
    assert a.default_model == "claude-opus-5"
    assert a.label.startswith("fallback:")
    g = _primary_from_row(
        _Row("Gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
        forced_model="gemini-2.5-flash",
    )
    assert g.default_model == "gemini-2.5-flash"


# ------------------------ Failover cruzado ------------------------


@pytest.mark.asyncio
async def test_claude_cae_y_responde_gpt_con_el_mismo_prompt_y_las_mismas_tools():
    claude = _Spy(exc=RuntimeError("529 overloaded"))
    gpt = _Spy(result=_ok("responde GPT"))
    out = await FallbackLLMProvider(claude, gpt).complete(MSGS, tools=TOOLS, model="claude-opus-5")

    assert out.content == "responde GPT"
    assert gpt.calls == 1
    # Mismo historial y mismas herramientas: solo cambia el backend.
    assert gpt.last_messages is MSGS
    assert gpt.last_tools is TOOLS
    # Y el respaldo usa SU modelo (model=None → su default), no el de Claude.
    assert gpt.last_model is None


@pytest.mark.asyncio
async def test_gpt_cae_y_responde_claude():
    """El sentido contrario también: el primario puede ser cualquiera."""
    gpt = _Spy(exc=RuntimeError("timeout"))
    claude = _Spy(result=_ok("responde Claude"))
    out = await FallbackLLMProvider(gpt, claude).complete(MSGS, tools=TOOLS, model="gpt-5.4-mini")
    assert out.content == "responde Claude"
    assert claude.calls == 1


@pytest.mark.asyncio
async def test_gemini_puede_ser_el_respaldo_de_claude():
    claude = _Spy(exc=RuntimeError("boom"))
    gemini = _Spy(result=_ok("responde Gemini"))
    out = await FallbackLLMProvider(claude, gemini).complete(MSGS, tools=TOOLS)
    assert out.content == "responde Gemini"


@pytest.mark.asyncio
async def test_claude_sin_clave_tambien_dispara_el_respaldo():
    """«El primario no tiene clave» es un motivo de failover como cualquier
    otro, no una respuesta con texto de aviso que se le pueda entregar al
    cliente final del negocio."""
    claude = _Spy(exc=LLMNotConfiguredError("sin clave"))
    gpt = _Spy(result=_ok("responde GPT"))
    out = await FallbackLLMProvider(claude, gpt).complete(MSGS, tools=TOOLS)
    assert out.content == "responde GPT"


@pytest.mark.asyncio
async def test_si_el_respaldo_no_esta_configurado_se_propaga_el_error_del_primario():
    """Sin respaldo, el error sube y el flujo de conversación deriva a una
    persona. Nunca se responde con un mensaje interno."""
    claude = _Spy(exc=RuntimeError("529 overloaded"))
    gpt = _Spy(configured=False)
    with pytest.raises(RuntimeError, match="529"):
        await FallbackLLMProvider(claude, gpt).complete(MSGS, tools=TOOLS)
    assert gpt.calls == 0


@pytest.mark.asyncio
async def test_si_el_primario_responde_no_se_toca_el_respaldo():
    claude = _Spy(result=_ok("responde Claude"))
    gpt = _Spy(result=_ok("no deberia"))
    out = await FallbackLLMProvider(claude, gpt).complete(MSGS, tools=TOOLS)
    assert out.content == "responde Claude"
    assert gpt.calls == 0


@pytest.mark.asyncio
async def test_si_tambien_falla_el_respaldo_sube_el_error_del_respaldo():
    claude = _Spy(exc=RuntimeError("primario"))
    gpt = _Spy(exc=RuntimeError("secundario"))
    with pytest.raises(RuntimeError, match="secundario"):
        await FallbackLLMProvider(claude, gpt).complete(MSGS, tools=TOOLS)
