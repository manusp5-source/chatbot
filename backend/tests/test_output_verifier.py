"""Verificador determinista de importes: lo que el agente cobra tiene que estar escrito.

Caso de origen: F2 del arnés. Preguntado por un implante de circonio (ausente de la KB), el
agente contestó con el precio de la porcelana. El prompt no lo evitó; esto es la segunda
barrera, y no depende del modelo.
"""

import json
from decimal import Decimal

import pytest

from app.agents import orchestrator
from app.agents.output_verifier import (
    TEXTO_IMPORTE_NO_RESPALDADO,
    TEXTO_IMPORTE_NO_RESPALDADO_SIN_DERIVAR,
    extraer_importes,
    importes_no_respaldados,
)
from app.providers.llm.base import LLMCompletion, LLMToolCall

# --- extracción ---------------------------------------------------------------


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("Cuesta 1.200 €", {Decimal("1200")}),
        ("Son 1200€ en total", {Decimal("1200")}),
        ("Desde 35 euros", {Decimal("35")}),
        ("Unos 1.200,50 euros", {Decimal("1200.50")}),
        ("Vale 49,90 EUR", {Decimal("49.90")}),
        ("Cuesta € 80", {Decimal("80")}),
        ("Entre 300 € y 450 €", {Decimal("300"), Decimal("450")}),
    ],
)
def test_extrae_importes_en_formato_espanol(texto, esperado) -> None:
    assert extraer_importes(texto) == esperado


@pytest.mark.parametrize(
    "texto",
    [
        "Te espero el jueves a las 10:30",
        "Estamos en la calle Mayor 12, 2º B",
        "Llama al 912 345 678",
        "Son 3 sesiones de 45 minutos",
        "",
    ],
)
def test_no_confunde_otros_numeros_con_importes(texto) -> None:
    assert extraer_importes(texto) == set()


# --- respaldo -----------------------------------------------------------------


def test_un_importe_que_esta_en_la_evidencia_esta_respaldado() -> None:
    evidencia = "Implante con corona de porcelana: 1.200 €"
    assert importes_no_respaldados("Cuesta 1.200 €", evidencia) == set()


def test_el_formato_no_importa_para_el_respaldo() -> None:
    assert importes_no_respaldados("Son 1.200 €", "precio: 1200 euros") == set()
    assert importes_no_respaldados("Son 1200€", "precio: 1.200,00 €") == set()


def test_un_importe_ausente_de_la_evidencia_no_esta_respaldado() -> None:
    evidencia = "Implante con corona de porcelana: 1.200 €"
    assert importes_no_respaldados("Cuesta 1.500 €", evidencia) == {Decimal("1500")}


def test_solo_se_marcan_los_importes_sin_respaldo() -> None:
    evidencia = "Limpieza: 60 €"
    assert importes_no_respaldados("Limpieza 60 € y blanqueamiento 250 €", evidencia) == {
        Decimal("250")
    }


# --- integración con run_agent -----------------------------------------------


class _Proveedor:
    """Modelo falso: primero llama a la tool (si se pide) y luego contesta."""

    def __init__(self, respuesta_final: str, *, llamar_kb: bool = False) -> None:
        self.respuesta_final = respuesta_final
        self.llamar_kb = llamar_kb
        self.turnos = 0

    async def complete(self, messages, **_kwargs):
        self.turnos += 1
        if self.llamar_kb and self.turnos == 1:
            return LLMCompletion(
                content=None,
                tool_calls=[LLMToolCall(id="t1", name="consultar_kb", arguments={"query": "x"})],
            )
        return LLMCompletion(content=self.respuesta_final, tool_calls=[])


async def _ejecutar(monkeypatch, *, respuesta, kb_devuelve=None, tools=None, historial=None):
    proveedor = _Proveedor(respuesta, llamar_kb=kb_devuelve is not None)

    async def _resolver(*_a, **_k):
        return proveedor

    async def _kb(_args, _ctx):
        return json.dumps(kb_devuelve)

    decisiones: list[str] = []

    async def _decision(*, decision, **_k):
        decisiones.append(decision)

    async def _sin_fecha():
        return ""

    monkeypatch.setattr(orchestrator, "resolve_llm_provider", _resolver)
    monkeypatch.setattr(orchestrator, "_linea_de_hoy", _sin_fecha)
    monkeypatch.setattr(orchestrator, "log_router_decision", _decision)
    monkeypatch.setattr(orchestrator.ALL_TOOLS["consultar_kb"], "handler", _kb)

    salida = await orchestrator.run_agent(
        system_prompt="Eres el asistente de la clínica.",
        history=historial or [],
        user_message="¿cuánto cuesta un implante con corona de circonio?",
        tools_enabled=tools if tools is not None else ["consultar_kb", "derivar_humano"],
    )
    return salida, decisiones


@pytest.mark.asyncio
async def test_un_precio_inventado_se_sustituye_por_el_texto_fijo(monkeypatch) -> None:
    """El caso F2: la KB solo trae la porcelana y el modelo vende el circonio a ese precio."""
    kb = {"results": [{"contenido": "Implante con corona de porcelana: 1.200 €"}]}

    salida, decisiones = await _ejecutar(
        monkeypatch, respuesta="Sí, el implante de circonio cuesta 1.900 €.", kb_devuelve=kb
    )

    assert salida == TEXTO_IMPORTE_NO_RESPALDADO
    assert "1.900" not in salida
    assert "salida_importe_no_respaldado" in decisiones


@pytest.mark.asyncio
async def test_un_precio_respaldado_por_la_tool_pasa_intacto(monkeypatch) -> None:
    kb = {"results": [{"contenido": "Implante con corona de porcelana: 1.200 €"}]}
    respuesta = "El implante con corona de porcelana cuesta 1.200 €."

    salida, decisiones = await _ejecutar(monkeypatch, respuesta=respuesta, kb_devuelve=kb)

    assert salida == respuesta
    assert decisiones == []


@pytest.mark.asyncio
async def test_una_respuesta_sin_importes_no_se_toca(monkeypatch) -> None:
    respuesta = "Eso no lo tengo a mano; te paso con el equipo."

    salida, decisiones = await _ejecutar(monkeypatch, respuesta=respuesta, kb_devuelve=None)

    assert salida == respuesta
    assert decisiones == []


@pytest.mark.asyncio
async def test_repetir_un_precio_dado_antes_en_la_conversacion_no_se_bloquea(monkeypatch) -> None:
    """Sin esto, el agente no podría repetir un precio sin volver a consultar la KB."""
    from app.providers.llm.base import LLMMessage

    historial = [
        LLMMessage(role="user", content="¿cuánto cuesta la limpieza?"),
        LLMMessage(role="assistant", content="La limpieza cuesta 60 €."),
    ]
    respuesta = "Como te decía, la limpieza son 60 €."

    salida, _ = await _ejecutar(
        monkeypatch, respuesta=respuesta, kb_devuelve=None, historial=historial
    )

    assert salida == respuesta


@pytest.mark.asyncio
async def test_sin_poder_derivar_el_texto_ofrece_devolver_la_llamada(monkeypatch) -> None:
    """El agente de voz no tiene `derivar_humano`: no puede prometer pasar a nadie."""
    salida, _ = await _ejecutar(
        monkeypatch,
        respuesta="Cuesta 3.000 €.",
        kb_devuelve={"results": []},
        tools=["consultar_kb"],
    )

    assert salida == TEXTO_IMPORTE_NO_RESPALDADO_SIN_DERIVAR
    assert "llam" in salida.lower()
    assert "persona" not in salida.lower().replace("una persona del equipo", "")


# --- consultar_kb sin resultados ---------------------------------------------


@pytest.mark.asyncio
async def test_consultar_kb_sin_resultados_dice_expresamente_que_no_hay(monkeypatch) -> None:
    from app.agents.tools import kb_search

    class _Filas:
        def fetchall(self):
            return []

    class _Sesion:
        async def execute(self, *_a, **_k):
            return _Filas()

    class _Ctx:
        async def __aenter__(self):
            return _Sesion()

        async def __aexit__(self, *_a):
            return False

    async def _emb(_textos):
        return [[0.1] * 1536]

    async def _nada(*_a, **_k):
        return None

    monkeypatch.setattr(kb_search, "db_session", lambda: _Ctx())
    monkeypatch.setattr(kb_search, "embed_texts", _emb)
    monkeypatch.setattr(kb_search, "log_kb_lookup", _nada)
    monkeypatch.setattr(kb_search, "_record_knowledge_gap_kb_miss", _nada)

    salida = json.loads(await kb_search.consultar_kb({"query": "circonio"}, {}))

    assert salida["results"] == []
    assert salida["sin_resultados"] is True
    assert "no inventes" in salida["instruccion"].lower()
