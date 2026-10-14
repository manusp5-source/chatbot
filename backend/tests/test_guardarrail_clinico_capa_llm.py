"""Capa 2 del guardarraíl clínico: un clasificador LLM que SOLO puede añadir derivaciones.

Las reglas (capa 1) son el suelo y no se tocan: auditables, gratis, y funcionan con el
proveedor caído. La capa 2 existe para lo que las reglas no pueden ver: paráfrasis que ninguna
lista prevé. Tres propiedades que estas pruebas fijan, porque son las que hacen que añadir un
LLM aquí no sea una regresión disfrazada de mejora:

  1. SOLO AÑADE. Si las reglas derivan, el LLM ni se consulta; si el LLM dice NO, el mensaje
     sigue su curso. Nunca anula una derivación de las reglas.
  2. FAIL-OPEN sobre la capa 2. Si el clasificador falla, se sigue como si no existiera: el
     suelo determinista sigue mandando y el paciente no se queda sin respuesta por un 500.
  3. APAGADA por defecto. Cuesta una llamada por mensaje; se activa a propósito.
"""

import pytest

from app.agents import guardarrail_clinico as g
from app.agents import orchestrator
from app.agents.guardarrail_clinico_llm import sospecha_clinica
from app.providers.llm.base import LLMCompletion, LLMMessage


class _Proveedor:
    """Modelo falso. El clasificador y el agente principal comparten proveedor."""

    def __init__(self, veredicto_clasificador="NO", *, falla_clasificador=False):
        self.veredicto_clasificador = veredicto_clasificador
        self.falla_clasificador = falla_clasificador
        self.llamadas_clasificador = 0
        self.llamadas_agente = 0

    async def complete(self, messages, **kwargs):
        es_clasificador = not kwargs.get("tools")
        if es_clasificador:
            self.llamadas_clasificador += 1
            if self.falla_clasificador:
                raise RuntimeError("proveedor caído")
            return LLMCompletion(content=self.veredicto_clasificador, tool_calls=[])
        self.llamadas_agente += 1
        return LLMCompletion(content="Claro, dime.", tool_calls=[])


async def _ejecutar(monkeypatch, proveedor, mensaje, *, capa_llm=True, tools=None, historial=None):
    async def _resolver(*_a, **_k):
        return proveedor

    async def _sin_fecha():
        return ""

    async def _nada(*_a, **_k):
        return None

    monkeypatch.setattr(orchestrator, "resolve_llm_provider", _resolver)
    monkeypatch.setattr(orchestrator, "_linea_de_hoy", _sin_fecha)
    monkeypatch.setattr(orchestrator, "log_router_decision", _nada)
    monkeypatch.setattr(orchestrator, "publish_agent_step", _nada)
    monkeypatch.setattr(orchestrator.settings, "GUARDARRAIL_CLINICO_LLM", capa_llm)

    return await orchestrator.run_agent(
        system_prompt="Eres el asistente de la clínica.",
        history=historial or [],
        user_message=mensaje,
        tools_enabled=tools if tools is not None else ["consultar_kb"],
    )


@pytest.mark.asyncio
async def test_el_clasificador_deriva_una_parafrasis_que_las_reglas_no_ven(monkeypatch) -> None:
    mensaje = "desde el tratamiento tengo la boca como dormida"
    assert g.evaluar(mensaje) is None  # las reglas no la ven: es justo el hueco de la capa 2
    proveedor = _Proveedor("SÍ")

    salida = await _ejecutar(monkeypatch, proveedor, mensaje)

    assert salida == g.MENSAJE_CLINICO
    assert proveedor.llamadas_agente == 0, "el agente principal no debe ver el mensaje"


@pytest.mark.asyncio
async def test_si_las_reglas_derivan_el_clasificador_ni_se_consulta(monkeypatch) -> None:
    proveedor = _Proveedor("NO")  # aunque dijera NO, no puede anular a las reglas

    salida = await _ejecutar(monkeypatch, proveedor, "me duele mucho la muela")

    assert salida == g.MENSAJE_CLINICO
    assert proveedor.llamadas_clasificador == 0


@pytest.mark.asyncio
async def test_si_el_clasificador_dice_no_el_mensaje_sigue_su_curso(monkeypatch) -> None:
    proveedor = _Proveedor("NO")

    salida = await _ejecutar(monkeypatch, proveedor, "quiero pedir cita para una limpieza")

    assert salida == "Claro, dime."
    assert proveedor.llamadas_clasificador == 1
    assert proveedor.llamadas_agente == 1


@pytest.mark.asyncio
async def test_si_el_clasificador_falla_se_sigue_sin_el_fail_open(monkeypatch) -> None:
    proveedor = _Proveedor(falla_clasificador=True)

    salida = await _ejecutar(monkeypatch, proveedor, "quiero pedir cita para una limpieza")

    assert salida == "Claro, dime."
    assert proveedor.llamadas_agente == 1


@pytest.mark.asyncio
async def test_con_la_capa_apagada_no_se_hace_ninguna_llamada_extra(monkeypatch) -> None:
    proveedor = _Proveedor("SÍ")

    salida = await _ejecutar(
        monkeypatch, proveedor, "desde el tratamiento tengo la boca como dormida", capa_llm=False
    )

    assert salida == "Claro, dime."
    assert proveedor.llamadas_clasificador == 0


@pytest.mark.asyncio
async def test_una_respuesta_ambigua_del_clasificador_se_trata_como_si(monkeypatch) -> None:
    """Ante la duda se deriva: la asimetría de costes del módulo entero."""
    proveedor = _Proveedor("quizá, no lo tengo claro")

    salida = await _ejecutar(monkeypatch, proveedor, "tengo una sensación rara")

    assert salida == g.MENSAJE_CLINICO


@pytest.mark.asyncio
async def test_sospecha_clinica_recibe_el_historial_reciente() -> None:
    visto = {}

    class _Espia:
        async def complete(self, messages, **_k):
            visto["texto"] = "\n".join(m.content or "" for m in messages)
            return LLMCompletion(content="NO", tool_calls=[])

    historial = [
        LLMMessage(role="user", content="hola, quiero una cita"),
        LLMMessage(role="assistant", content="Claro, ¿para qué día?"),
    ]
    await sospecha_clinica(_Espia(), None, historial, "pues me duele")

    assert "quiero una cita" in visto["texto"]
    assert "pues me duele" in visto["texto"]
