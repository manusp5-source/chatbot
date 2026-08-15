"""Proveedor Anthropic (Claude): traducción en los dos sentidos y errores.

La parte delicada es la traducción, porque Anthropic no se parece a OpenAI en
casi nada de lo que el orquestador da por supuesto: el `system` va aparte, el
rol `tool` no existe, y una llamada a herramienta es un BLOQUE dentro del
contenido del assistant en vez de un campo hermano. Si cualquiera de esas
conversiones se rompe, el agente deja de poder usar herramientas — que es todo
lo que hace: buscar en la base de conocimiento, guardar el contacto, derivar a
una persona.

Nada de esto toca la red: se inyecta un cliente falso.
"""
import pytest

from app.providers.llm.anthropic_client import (
    AnthropicProvider,
    _from_anthropic_content,
    _to_anthropic_messages,
    _to_anthropic_tool,
    rejects_temperature,
)
from app.providers.llm.base import (
    LLMMessage,
    LLMNotConfiguredError,
    LLMToolCall,
    LLMToolSchema,
)

# --------------------------- Dobles del SDK ---------------------------


class _Block:
    """Bloque de contenido tal y como lo devuelve el SDK (objeto, no dict)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Usage:
    def __init__(self, input_tokens=0, output_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Resp:
    def __init__(self, content, stop_reason="end_turn", usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage


class _FakeMessages:
    def __init__(self, sink, resp=None, exc=None):
        self.sink = sink
        self.resp = resp
        self.exc = exc

    async def create(self, **params):
        self.sink.update(params)
        if self.exc:
            raise self.exc
        return self.resp


class _FakeClient:
    def __init__(self, sink, resp=None, exc=None):
        self.messages = _FakeMessages(sink, resp, exc)


def _provider(monkeypatch, sink, resp=None, exc=None, api_key="k", **kw):
    p = AnthropicProvider(explicit_api_key=api_key or None, **kw)
    monkeypatch.setattr(p, "_client", lambda *a, **k: _FakeClient(sink, resp, exc))
    return p


def _text_resp(text="hola", pt=10, ct=5):
    return _Resp([_Block(type="text", text=text)], usage=_Usage(pt, ct))


# ------------------- Traducción de mensajes (ida) -------------------


def test_system_sale_de_messages_y_va_en_su_parametro():
    """En Anthropic un mensaje con role=system es un error: va aparte."""
    system, msgs = _to_anthropic_messages(
        [
            LLMMessage(role="system", content="Eres el bot de la tienda."),
            LLMMessage(role="user", content="hola"),
        ]
    )
    assert system == "Eres el bot de la tienda."
    assert msgs == [{"role": "user", "content": "hola"}]
    assert all(m["role"] != "system" for m in msgs)


def test_varios_system_se_concatenan():
    system, msgs = _to_anthropic_messages(
        [
            LLMMessage(role="system", content="uno"),
            LLMMessage(role="system", content="dos"),
            LLMMessage(role="user", content="hola"),
        ]
    )
    assert system == "uno\n\ndos"
    assert len(msgs) == 1


def test_assistant_con_tool_calls_se_convierte_en_bloques_tool_use():
    """El campo `tool_calls` de OpenAI pasa a ser un bloque del contenido."""
    _system, msgs = _to_anthropic_messages(
        [
            LLMMessage(role="user", content="¿horario?"),
            LLMMessage(
                role="assistant",
                content="Miro la ficha.",
                tool_calls=[
                    LLMToolCall(id="toolu_1", name="consultar_kb", arguments={"query": "horario"})
                ],
            ),
        ]
    )
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == [
        {"type": "text", "text": "Miro la ficha."},
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "consultar_kb",
            "input": {"query": "horario"},
        },
    ]


def test_rol_tool_se_convierte_en_tool_result_dentro_de_un_turno_de_usuario():
    """Anthropic no tiene rol `tool`: el resultado va como bloque en un user."""
    _system, msgs = _to_anthropic_messages(
        [
            LLMMessage(role="user", content="¿horario?"),
            LLMMessage(
                role="assistant",
                content=None,
                tool_calls=[LLMToolCall(id="toolu_1", name="kb", arguments={})],
            ),
            LLMMessage(role="tool", tool_call_id="toolu_1", content='{"ok":true}', name="kb"),
        ]
    )
    assert msgs[-1] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"ok":true}'}],
    }


def test_tool_results_consecutivos_se_agrupan_en_un_solo_turno():
    """Cuando el modelo pide dos herramientas a la vez, los dos resultados van
    en el MISMO turno de usuario: es la forma que documenta la API."""
    _system, msgs = _to_anthropic_messages(
        [
            LLMMessage(role="user", content="?"),
            LLMMessage(
                role="assistant",
                tool_calls=[
                    LLMToolCall(id="a", name="t1", arguments={}),
                    LLMToolCall(id="b", name="t2", arguments={}),
                ],
            ),
            LLMMessage(role="tool", tool_call_id="a", content="r1"),
            LLMMessage(role="tool", tool_call_id="b", content="r2"),
        ]
    )
    turnos_usuario_finales = [m for m in msgs if m["role"] == "user"]
    assert len(turnos_usuario_finales) == 2  # la pregunta + el turno de resultados
    assert [b["tool_use_id"] for b in msgs[-1]["content"]] == ["a", "b"]


def test_el_primer_turno_siempre_es_de_usuario():
    """Si el negocio abrió la conversación, el histórico empieza en assistant y
    Anthropic lo rechaza. Se antepone un turno de usuario en vez de perder el
    saludo."""
    _system, msgs = _to_anthropic_messages(
        [
            LLMMessage(role="system", content="s"),
            LLMMessage(role="assistant", content="¡Hola! ¿En qué te ayudo?"),
            LLMMessage(role="user", content="quiero un presupuesto"),
        ]
    )
    assert msgs[0]["role"] == "user"
    assert msgs[1]["content"] == [{"type": "text", "text": "¡Hola! ¿En qué te ayudo?"}]


def test_no_se_mandan_turnos_vacios():
    """Un bloque de texto vacío o un assistant sin nada es un 400."""
    _system, msgs = _to_anthropic_messages(
        [
            LLMMessage(role="user", content="hola"),
            LLMMessage(role="assistant", content="   "),
            LLMMessage(role="assistant", content=None),
        ]
    )
    assert msgs == [{"role": "user", "content": "hola"}]


def test_tool_result_vacio_no_manda_cadena_vacia():
    _system, msgs = _to_anthropic_messages(
        [LLMMessage(role="user", content="x"), LLMMessage(role="tool", tool_call_id="t", content="")]
    )
    assert msgs[-1]["content"][0]["content"] == "-"


def test_herramienta_usa_input_schema_no_parameters():
    schema = LLMToolSchema(
        name="consultar_kb",
        description="Busca en la base de conocimiento",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    assert _to_anthropic_tool(schema) == {
        "name": "consultar_kb",
        "description": "Busca en la base de conocimiento",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
    }
    # Y NO el envoltorio de OpenAI.
    assert "function" not in _to_anthropic_tool(schema)


# ------------------- Traducción de la respuesta (vuelta) -------------------


def test_respuesta_con_texto_y_tool_use_se_traduce_a_LLMToolCall():
    content, calls = _from_anthropic_content(
        [
            _Block(type="text", text="Voy a mirarlo."),
            _Block(type="tool_use", id="toolu_9", name="consultar_kb", input={"query": "precio"}),
        ]
    )
    assert content == "Voy a mirarlo."
    assert len(calls) == 1
    assert calls[0].id == "toolu_9"
    assert calls[0].name == "consultar_kb"
    # `arguments` tiene que llegar como DICT: es lo que el orquestador le pasa
    # al handler de la herramienta. Anthropic ya lo manda parseado.
    assert calls[0].arguments == {"query": "precio"}


def test_bloques_desconocidos_no_rompen_la_respuesta():
    """Si algún día se activa el thinking, no debe tumbar la conversación."""
    content, calls = _from_anthropic_content(
        [_Block(type="thinking", thinking="..."), _Block(type="text", text="hola")]
    )
    assert content == "hola"
    assert calls == []


def test_varios_bloques_de_texto_se_concatenan():
    content, _ = _from_anthropic_content(
        [_Block(type="text", text="uno"), _Block(type="text", text="dos")]
    )
    assert content == "uno\ndos"


# --------------------------- Llamada completa ---------------------------


@pytest.mark.asyncio
async def test_complete_manda_max_tokens_siempre():
    """`max_tokens` es OBLIGATORIO en la Messages API: si el agente no lo fija,
    hay que poner un valor o la llamada es un 400."""
    sink: dict = {}
    p = _provider(pytest.MonkeyPatch(), sink, resp=_text_resp())
    out = await p.complete([LLMMessage(role="user", content="hola")], model="claude-opus-4-8")
    assert sink["max_tokens"] > 0
    assert out.content == "hola"


@pytest.mark.asyncio
async def test_complete_traduce_usage_a_los_nombres_de_openai():
    """El resto de la app (usage_tracker, budget, panel) espera
    prompt_tokens/completion_tokens, no input_tokens/output_tokens."""
    sink: dict = {}
    p = _provider(pytest.MonkeyPatch(), sink, resp=_text_resp(pt=120, ct=30))
    out = await p.complete([LLMMessage(role="user", content="hola")], model="claude-opus-4-8")
    assert out.usage == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
    }


@pytest.mark.asyncio
async def test_stop_reason_se_traduce_al_vocabulario_del_panel():
    sink: dict = {}
    resp = _Resp([_Block(type="tool_use", id="t", name="kb", input={})], stop_reason="tool_use")
    p = _provider(pytest.MonkeyPatch(), sink, resp=resp)
    out = await p.complete([LLMMessage(role="user", content="x")], model="claude-opus-4-8")
    assert out.finish_reason == "tool_calls"


# --------------------------- temperature ---------------------------


def test_los_modelos_nuevos_rechazan_temperature():
    assert rejects_temperature("claude-opus-4-8")
    assert rejects_temperature("claude-opus-5")
    assert rejects_temperature("claude-sonnet-5")
    assert rejects_temperature("claude-fable-5")
    assert not rejects_temperature("claude-sonnet-4-6")
    assert not rejects_temperature("claude-haiku-4-5")


@pytest.mark.asyncio
async def test_no_se_manda_temperature_a_un_modelo_que_la_rechaza():
    """Aunque la casilla del panel esté marcada. Mandarla es un 400 y dejaría
    al agente sin responder a NADA, derivando todo a una persona."""
    sink: dict = {}
    p = _provider(pytest.MonkeyPatch(), sink, resp=_text_resp(), accepts_temperature=True)
    await p.complete(
        [LLMMessage(role="user", content="hola")], model="claude-opus-4-8", temperature=0.7
    )
    assert "temperature" not in sink


@pytest.mark.asyncio
async def test_si_se_manda_temperature_a_un_modelo_que_la_acepta():
    sink: dict = {}
    p = _provider(pytest.MonkeyPatch(), sink, resp=_text_resp(), accepts_temperature=True)
    await p.complete(
        [LLMMessage(role="user", content="hola")], model="claude-sonnet-4-6", temperature=0.4
    )
    assert sink["temperature"] == 0.4


@pytest.mark.asyncio
async def test_la_casilla_del_panel_tambien_puede_quitar_la_temperature():
    sink: dict = {}
    p = _provider(pytest.MonkeyPatch(), sink, resp=_text_resp(), accepts_temperature=False)
    await p.complete(
        [LLMMessage(role="user", content="hola")], model="claude-sonnet-4-6", temperature=0.4
    )
    assert "temperature" not in sink


# --------------------------- Errores ---------------------------


@pytest.mark.asyncio
async def test_sin_clave_lanza_en_vez_de_devolver_texto():
    """Nunca devolver un aviso del panel como si fuera la respuesta del bot:
    acabaría llegándole por WhatsApp al cliente final del negocio."""
    p = AnthropicProvider(explicit_api_key=None, api_key_credential="__no_existe__")
    with pytest.raises(LLMNotConfiguredError):
        await p.complete([LLMMessage(role="user", content="hola")], model="claude-opus-4-8")


@pytest.mark.asyncio
async def test_clave_invalida_propaga_el_error_para_que_salte_el_failover():
    """Un 401 de Anthropic tiene que subir tal cual: es lo que dispara el
    respaldo (o, si no lo hay, la derivación a una persona)."""

    class _AuthError(Exception):
        pass

    sink: dict = {}
    p = _provider(pytest.MonkeyPatch(), sink, exc=_AuthError("401 invalid x-api-key"))
    with pytest.raises(_AuthError):
        await p.complete([LLMMessage(role="user", content="hola")], model="claude-opus-4-8")


@pytest.mark.asyncio
async def test_is_configured_solo_pide_la_clave():
    assert await AnthropicProvider(explicit_api_key="k").is_configured() is True
    assert (
        await AnthropicProvider(
            explicit_api_key=None, api_key_credential="__no_existe__"
        ).is_configured()
        is False
    )
