"""Tarifas de Claude y Gemini: mapeo con OpenRouter y semilla oficial.

Por qué esto importa tanto: un modelo sin precio no cuesta 0 de verdad, cuesta
0 EN LA CUENTA. Como el tope mensual se calcula sumando el coste estimado de
cada llamada, un modelo que no case con la tabla de precios suma 0 al gasto del
mes, el tope nunca salta y el agente sigue respondiendo (y facturando) como si
fuera gratis. Con Claude el riesgo es real porque los ids NO casan por sufijo.
"""
import pytest

from app.services.llm_pricing import PRICE_PER_1M_USD, estimate_cost_usd
from app.services.openrouter_pricing import _match_candidates, _suffix

# ------------------------------------------------------------------
# Ids reales del listado público de OpenRouter, capturados el 11-ago-2026
# (403 modelos). Anthropic separa la versión con PUNTO aquí y con GUION en su
# propia API; Google la separa con punto en los dos sitios.
# ------------------------------------------------------------------
OPENROUTER_IDS = [
    "anthropic/claude-fable-5",
    "anthropic/claude-opus-5",
    "anthropic/claude-sonnet-5",
    "anthropic/claude-opus-4.8",
    "anthropic/claude-opus-4.7",
    "anthropic/claude-opus-4.6",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-haiku-4.5",
    "google/gemini-3.6-flash",
    "google/gemini-3.5-flash",
    "google/gemini-3.1-pro-preview",
    "google/gemini-2.5-pro",
    "google/gemini-2.5-flash",
    "openai/gpt-5.4-mini",
]

# id de la API del proveedor → id de OpenRouter que le corresponde.
MAPEO_ESPERADO = {
    # Los que NO casan por sufijo: guion contra punto.
    "claude-opus-4-8": "anthropic/claude-opus-4.8",
    "claude-opus-4-7": "anthropic/claude-opus-4.7",
    "claude-opus-4-6": "anthropic/claude-opus-4.6",
    "claude-sonnet-4-6": "anthropic/claude-sonnet-4.6",
    "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
    # Id fijado con fecha: además hay que quitarle el `-AAAAMMDD`.
    "claude-haiku-4-5-20251001": "anthropic/claude-haiku-4.5",
    # Los de una sola cifra sí casan tal cual.
    "claude-opus-5": "anthropic/claude-opus-5",
    "claude-sonnet-5": "anthropic/claude-sonnet-5",
    "claude-fable-5": "anthropic/claude-fable-5",
    # Gemini casa tal cual; solo hay que quitar el prefijo del listado.
    "gemini-2.5-flash": "google/gemini-2.5-flash",
    "gemini-3.1-pro-preview": "google/gemini-3.1-pro-preview",
    "models/gemini-2.5-flash": "google/gemini-2.5-flash",
    # Lo que ya funcionaba tiene que seguir funcionando.
    "gpt-5.4-mini": "openai/gpt-5.4-mini",
}


def _indice():
    by_suffix = {}
    for mid in OPENROUTER_IDS:
        by_suffix.setdefault(_suffix(mid), mid)
    return by_suffix


@pytest.mark.parametrize("modelo,esperado", MAPEO_ESPERADO.items())
def test_cada_modelo_encuentra_su_id_de_openrouter(modelo, esperado):
    by_suffix = _indice()
    match = next((by_suffix[c] for c in _match_candidates(modelo) if c in by_suffix), None)
    assert match == esperado, f"{modelo} no casó (candidatos: {_match_candidates(modelo)})"


def test_el_sufijo_solo_no_habria_bastado_para_claude():
    """Regresión explícita del fallo que se está corrigiendo: con la regla
    anterior (un único sufijo) los Claude con dos cifras quedaban sin tarifa."""
    by_suffix = _indice()
    assert "claude-opus-4-8" not in by_suffix
    assert _suffix("claude-opus-4-8") not in by_suffix


def test_un_modelo_inventado_sigue_sin_casar():
    """Las reglas de traducción no pueden inventarse una correspondencia."""
    by_suffix = _indice()
    assert (
        next((by_suffix[c] for c in _match_candidates("modelo-que-no-existe-9-9") if c in by_suffix), None)
        is None
    )


# ------------------------------------------------------------------
# Semilla: contrastada con las tarifas OFICIALES de cada fabricante el
# 11-ago-2026, no con lo que publique un intermediario.
#   Anthropic: platform.claude.com/docs/en/about-claude/models/overview
#   Google:    ai.google.dev/gemini-api/docs/pricing (nivel de pago, ≤200k)
# ------------------------------------------------------------------
TARIFA_OFICIAL = {
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),  # de lista; 2/10 es promo hasta 31-ago-2026
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "gemini-3.6-flash": (1.50, 7.50),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}


@pytest.mark.parametrize("modelo,precio", TARIFA_OFICIAL.items())
def test_la_semilla_coincide_con_la_tarifa_oficial(modelo, precio):
    inp, out = precio
    assert modelo in PRICE_PER_1M_USD, f"{modelo} no está en la semilla de precios"
    assert PRICE_PER_1M_USD[modelo]["input"] == inp
    assert PRICE_PER_1M_USD[modelo]["output"] == out


@pytest.mark.parametrize("modelo", list(TARIFA_OFICIAL))
def test_ningun_modelo_nuevo_sale_gratis_desde_el_dia_uno(modelo):
    """Sin semilla, la PRIMERA jornada de cada instalación contaría 0 € para
    estos modelos y dependería de que el cron de precios estuviera vivo."""
    assert estimate_cost_usd(modelo, 2000, 500, PRICE_PER_1M_USD) > 0


def test_claude_no_se_infravalora_frente_al_modelo_por_defecto():
    """Opus cuesta bastante más que el modelo por defecto: si alguien cambia el
    agente a Claude sin tocar el tope, tiene que notarse en la cuenta."""
    opus = estimate_cost_usd("claude-opus-5", 2000, 500, PRICE_PER_1M_USD)
    por_defecto = estimate_cost_usd("gpt-5.4-mini", 2000, 500, PRICE_PER_1M_USD)
    assert opus > por_defecto


def test_un_modelo_de_claude_sin_tarifa_se_estima_por_lo_alto():
    """Antes costaba 0, y eso dejaba ciego al tope de presupuesto: el gasto del
    mes es la suma de estos costes. Ahora se estima con la tarifa de respaldo y
    el aviso de que está pasando lo da providers/llm/telemetry.py."""
    from app.services.llm_pricing import PRECIO_RESPALDO

    coste = estimate_cost_usd("claude-opus-9-9", 1000, 1000, PRICE_PER_1M_USD)
    esperado = (1000 * PRECIO_RESPALDO["input"] + 1000 * PRECIO_RESPALDO["output"]) / 1_000_000
    assert abs(coste - esperado) < 1e-12
    assert coste > 0
