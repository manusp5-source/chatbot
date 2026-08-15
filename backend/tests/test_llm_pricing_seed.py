"""Precios de la semilla: contrastados con la tarifa pública de OpenAI.

La semilla tenía los tres GPT-5.4 mal. El más grave era el modelo POR DEFECTO,
gpt-5.4-mini: figuraba a 0,15/0,60 cuando cuesta 0,75/4,50, así que el tope de
gasto medía cinco veces menos en entrada y siete y media menos en salida. Un
tope de 50 $ dejaba gastar unos 300 $ reales antes de cortar.

Se autocorregía con el refresco diario desde OpenRouter, pero la primera jornada
de cada instalación iba mal y dependía de que el servicio de tareas programadas
estuviera desplegado y vivo.
"""
import pytest

from app.services.llm_pricing import PRICE_PER_1M_USD, estimate_cost_usd

# Tarifa pública de OpenAI, USD por 1M de tokens (input, output),
# verificada en developers.openai.com/api/docs/pricing el 11-ago-2026.
TARIFA_OFICIAL = {
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
    "gpt-4o-mini-transcribe": (1.25, 5.00),
    "gpt-4o-transcribe": (2.50, 10.00),
}


@pytest.mark.parametrize("model,precio", TARIFA_OFICIAL.items())
def test_semilla_coincide_con_la_tarifa_oficial(model, precio):
    inp, out = precio
    assert model in PRICE_PER_1M_USD, f"{model} no está en la semilla de precios"
    assert PRICE_PER_1M_USD[model]["input"] == inp
    assert PRICE_PER_1M_USD[model]["output"] == out


def test_el_modelo_por_defecto_no_esta_infravalorado():
    """Regresión directa del fallo: 1M de tokens de salida del modelo por
    defecto cuestan 4,50 $, no 0,60 $."""
    from app.core.config import settings

    modelo = settings.DEFAULT_LLM_MODEL
    coste = estimate_cost_usd(modelo, 0, 1_000_000, PRICE_PER_1M_USD)
    assert coste == pytest.approx(4.50)
    # Y el mensaje típico (2k entrada + 500 salida) no sale prácticamente gratis.
    real = estimate_cost_usd(modelo, 2000, 500, PRICE_PER_1M_USD)
    viejo = estimate_cost_usd(
        "viejo", 2000, 500, {"viejo": {"input": 0.15, "output": 0.60}}
    )
    assert real > viejo * 4


def test_la_transcripcion_ya_no_sale_gratis():
    """Sin estas filas, cada nota de voz contaba 0 $ contra el tope."""
    from app.core.config import settings

    modelo = settings.OPENAI_WHISPER_MODEL
    assert (
        estimate_cost_usd(modelo, 10_000, 500, PRICE_PER_1M_USD) > 0
    ), f"el modelo de transcripción {modelo} no tiene precio en la tabla"


def test_modelo_desconocido_se_estima_en_vez_de_contar_cero():
    """Contar 0 dejaba el tope de presupuesto sin saltar nunca."""
    from app.services.llm_pricing import PRECIO_RESPALDO

    coste = estimate_cost_usd("modelo-inexistente", 1000, 1000, PRICE_PER_1M_USD)
    esperado = (1000 * PRECIO_RESPALDO["input"] + 1000 * PRECIO_RESPALDO["output"]) / 1_000_000
    assert abs(coste - esperado) < 1e-12
