"""Lista blanca de tools: "sin configurar" (None) vs "ninguna" ([]).

Desmarcar TODAS las casillas en el panel mandaba una lista vacía, y el
orquestador hacía `if not tools_enabled: return set(ALL_TOOLS)`. Una lista vacía
es falsy, así que se activaban TODAS: quien quería un agente que solo
conversara acababa con uno que agendaba citas en Google y derivaba
conversaciones.
"""
from app.agents.orchestrator import _resolve_allowed_tools
from app.agents.tools import ALL_TOOLS


def test_none_es_sin_configurar_y_habilita_todas():
    assert _resolve_allowed_tools(None) == set(ALL_TOOLS.keys())


def test_lista_vacia_es_ninguna():
    assert _resolve_allowed_tools([]) == set()


def test_lista_concreta_se_respeta():
    assert _resolve_allowed_tools(["consultar_kb"]) == {"consultar_kb"}


def test_nombres_inexistentes_se_ignoran_sin_romper():
    assert _resolve_allowed_tools(["consultar_kb", "tool_que_no_existe"]) == {
        "consultar_kb"
    }
    # Y una lista SOLO de nombres inventados no abre la puerta a todas.
    assert _resolve_allowed_tools(["inventada"]) == set()


def test_agente_de_voz_no_puede_derivar():
    """La lista del agente de voz excluye `derivar_humano` a propósito."""
    from app.scripts.seed import VOICE_AGENT_TOOLS

    assert "derivar_humano" not in _resolve_allowed_tools(VOICE_AGENT_TOOLS)
