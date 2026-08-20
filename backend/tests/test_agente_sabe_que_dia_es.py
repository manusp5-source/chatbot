"""El agente tiene que saber qué día es antes de decir si el centro está abierto.

Sin esto, a "¿estáis abiertos?" el agente contestaba con el horario semanal y
afirmaba que sí, incluso teniendo delante el documento de la base de conocimiento
que decía que la clínica cierra las dos últimas semanas de agosto. Contradecir el
documento que se acaba de leer es el peor fallo posible: el dato correcto estaba
ahí.

La fecha se antepone al prompt del sistema en `run_agent`, así que la reciben
todos los canales a la vez (texto y voz) sin que cada uno se acuerde de pasarla.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.agents import orchestrator
from app.providers.llm.base import LLMCompletion


class _ConfigFalsa:
    """Lo mínimo que `_linea_de_hoy` le pide a la config de horario."""

    def __init__(self, tz_name: str) -> None:
        self.tz_name = tz_name
        self.tz = ZoneInfo(tz_name)


@pytest.mark.asyncio
async def test_la_linea_dice_el_dia_el_mes_y_la_hora(monkeypatch):
    """Jueves 20 de agosto de 2026, 10:25 en Madrid: eso es lo que debe leer."""
    momento = datetime(2026, 8, 20, 10, 25, tzinfo=ZoneInfo("Europe/Madrid"))

    async def _config():
        return _ConfigFalsa("Europe/Madrid")

    monkeypatch.setattr(
        "app.agents.tools.schedule_config.get_schedule_config", _config
    )
    monkeypatch.setattr(
        "app.agents.tools.schedule_config.now_in", lambda tz: momento
    )

    linea = await orchestrator._linea_de_hoy()

    assert "jueves" in linea
    assert "20 de agosto" in linea
    assert "2026" in linea
    assert "10:25" in linea
    assert "Europe/Madrid" in linea
    # Y le dice explícitamente que no la adivine.
    assert "no la adivines" in linea


@pytest.mark.asyncio
async def test_usa_la_zona_del_negocio_y_no_la_del_servidor(monkeypatch):
    """El contenedor va en UTC; el centro, en su zona. Manda la del centro.

    Con un servidor en UTC, las 23:30 de un jueves en Madrid son las 21:30 del
    mismo jueves en UTC — pero a las 00:30 del viernes en Madrid, en UTC sigue
    siendo jueves. Contestar con el día del servidor le cambia el día al
    paciente.
    """
    medianoche_madrid = datetime(
        2026, 8, 21, 0, 30, tzinfo=ZoneInfo("Europe/Madrid")
    )

    async def _config():
        return _ConfigFalsa("Europe/Madrid")

    monkeypatch.setattr(
        "app.agents.tools.schedule_config.get_schedule_config", _config
    )
    monkeypatch.setattr(
        "app.agents.tools.schedule_config.now_in", lambda tz: medianoche_madrid
    )

    linea = await orchestrator._linea_de_hoy()

    assert "viernes" in linea, "debe usar el día del negocio, no el del servidor"
    assert "21 de agosto" in linea


@pytest.mark.asyncio
async def test_si_no_se_puede_saber_la_fecha_se_contesta_igual(monkeypatch):
    """Saber la fecha es una mejora, no un requisito para atender.

    Si la config de horario revienta —base de datos caída, zona mal escrita—, el
    agente tiene que seguir contestando: devolver cadena vacía deja el prompt
    exactamente como estaba antes de este cambio.
    """

    async def _revienta():
        raise RuntimeError("la base de datos no responde")

    monkeypatch.setattr(
        "app.agents.tools.schedule_config.get_schedule_config", _revienta
    )

    linea = await orchestrator._linea_de_hoy()

    assert linea == ""


@pytest.mark.asyncio
async def test_la_fecha_llega_al_prompt_del_sistema(monkeypatch):
    """La comprobación que de verdad importa: que el modelo la reciba.

    Se sustituye el proveedor de modelo por uno que guarda lo que le llega, y se
    afirma sobre el mensaje `system` real, no sobre el helper por separado.
    """
    momento = datetime(2026, 8, 20, 10, 25, tzinfo=ZoneInfo("Europe/Madrid"))

    async def _config():
        return _ConfigFalsa("Europe/Madrid")

    monkeypatch.setattr(
        "app.agents.tools.schedule_config.get_schedule_config", _config
    )
    monkeypatch.setattr(
        "app.agents.tools.schedule_config.now_in", lambda tz: momento
    )

    visto: dict = {}

    class _ProveedorEspia:
        async def complete(self, messages, **kwargs):
            visto["messages"] = messages
            return LLMCompletion(content="Hola.", tool_calls=[])

    async def _resolver(*a, **k):
        return _ProveedorEspia()

    monkeypatch.setattr(orchestrator, "resolve_llm_provider", _resolver)

    await orchestrator.run_agent(
        system_prompt="Eres el asistente de la clínica.",
        history=[],
        user_message="hola, estáis abiertos?",
        tools_enabled=[],
    )

    sistema = visto["messages"][0]
    assert sistema.role == "system"
    assert "jueves" in sistema.content
    assert "20 de agosto" in sistema.content
    # Y el prompt original sigue entero detrás de la fecha.
    assert sistema.content.endswith("Eres el asistente de la clínica.")
