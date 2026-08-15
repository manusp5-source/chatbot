"""Precios por modelo (USD por 1M tokens) para estimar el coste del consumo LLM.

Los precios viven en la tabla `llm_model_price` (editable desde el panel y
auto-actualizable desde OpenRouter). Aquí exponemos:

- `estimate_cost_usd(model, pt, ct, prices=None)`: SÍNCRONO. Usa el `prices`
  que se le pase o, si no, una caché en memoria del proceso. Se mantiene síncrono
  porque lo llaman rutas calientes (logging por llamada, budget) que no siempre
  están en contexto async con una sesión a mano.
- `get_price_map(db)`: ASÍNCRONO. Lee la tabla y la fusiona sobre los defaults.
  Lo usan los sitios que quieren el precio EXACTO al vuelo (dashboard, budget).
- `refresh_price_cache(db)`: recarga la caché del proceso (al arrancar la app y
  tras cada actualización de precios).

Los defaults (`PRICE_PER_1M_USD`) son la semilla/último recurso: si un modelo no
está ni en BD ni aquí, su coste estimado es 0 (no inventamos coste).
"""
from __future__ import annotations

from app.core.logging import get_logger

logger = get_logger(__name__)

# USD por 1M tokens. (input, output). Semilla / fallback si la BD no responde o
# el modelo no está registrado. La fuente de verdad es la tabla llm_model_price.
#
# CONTRASTADOS con la tarifa pública de OpenAI (developers.openai.com/api/docs/
# pricing) el 11-ago-2026. Los tres GPT-5.4 estaban mal desde el principio: el
# modelo POR DEFECTO (gpt-5.4-mini) figuraba a 0,15/0,60 cuando cuesta 0,75/4,50
# — infravalorado 5x en entrada y 7,5x en salida. Con eso, un tope de 50 $ dejaba
# gastar unos 300 $ reales. El refresco diario desde OpenRouter lo corregía, pero
# la PRIMERA jornada de cada instalación iba mal y dependía de que el servicio de
# tareas programadas estuviera desplegado y vivo.
#
# Los modelos de transcripción están aquí porque su consumo tampoco se contaba:
# se facturan por tokens de audio (el "$/minuto" que publica OpenAI es una
# estimación derivada de eso).
PRICE_PER_1M_USD: dict[str, dict[str, float]] = {
    "gpt-5.4": {"input": 2.50, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.75, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "output": 1.25},
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
    "text-embedding-3-large": {"input": 0.13, "output": 0.0},
    "gpt-4o-mini-transcribe": {"input": 1.25, "output": 5.00},
    "gpt-4o-transcribe": {"input": 2.50, "output": 10.00},
    "omni-moderation-latest": {"input": 0.0, "output": 0.0},
    # --- Anthropic (Claude) ---
    # Tarifa pública de platform.claude.com/docs/en/about-claude/models/overview,
    # verificada el 11-ago-2026. Coincide con lo que publica OpenRouter para los
    # mismos modelos, así que el refresco diario no los va a mover.
    "claude-fable-5": {"input": 10.00, "output": 50.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    # OJO: 3/15 es la tarifa de LISTA. Anthropic aplica un precio de
    # lanzamiento de 2/10 hasta el 31-ago-2026 y OpenRouter publica ESE, así
    # que el refresco diario bajará esta fila. Se siembra la de lista a
    # propósito: para un tope de gasto, quedarse corto es el error peligroso.
    "claude-sonnet-5": {"input": 3.00, "output": 15.00},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},
    "claude-opus-4-7": {"input": 5.00, "output": 25.00},
    "claude-opus-4-6": {"input": 5.00, "output": 25.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    # --- Google (Gemini) ---
    # Tarifa pública de ai.google.dev/gemini-api/docs/pricing (nivel de pago),
    # verificada el 11-ago-2026. Es el tramo de contexto CORTO (≤200k): los
    # modelos Pro cobran más a partir de ahí (gemini-3.1-pro-preview pasa de
    # 2/12 a 4/18) y el audio de entrada también va aparte. Ni OpenRouter ni
    # esta tabla modelan tramos, así que en conversaciones muy largas o con
    # muchas notas de voz el coste real puede quedar por debajo del estimado.
    "gemini-3.6-flash": {"input": 1.50, "output": 7.50},
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
    "gemini-3.1-pro-preview": {"input": 2.00, "output": 12.00},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
}

# Caché del proceso: arranca con los defaults y se recarga desde BD al inicio y
# tras cada actualización de precios.
_PRICE_CACHE: dict[str, dict[str, float]] = dict(PRICE_PER_1M_USD)


async def get_price_map(db) -> dict[str, dict[str, float]]:
    """Mapa {modelo: {input, output}} leído de BD, fusionado sobre los defaults.

    La BD gana sobre la semilla. Nunca lanza: si la lectura falla, devuelve al
    menos los defaults (no queremos tumbar el dashboard por esto).
    """
    prices: dict[str, dict[str, float]] = dict(PRICE_PER_1M_USD)
    try:
        from sqlalchemy import select

        from app.models.llm_model_price import LLMModelPrice

        rows = (await db.execute(select(LLMModelPrice))).scalars().all()
        for r in rows:
            prices[r.model] = {
                "input": float(r.input_per_1m or 0),
                "output": float(r.output_per_1m or 0),
            }
    except Exception as e:  # pragma: no cover - defensivo
        logger.warning("pricing.get_price_map.error", error=str(e))
    return prices


async def refresh_price_cache(db=None) -> None:
    """Recarga la caché del proceso desde BD. Si no se pasa `db`, abre sesión."""
    global _PRICE_CACHE
    try:
        if db is not None:
            _PRICE_CACHE = await get_price_map(db)
        else:
            from app.db.session import db_session

            async with db_session() as s:
                _PRICE_CACHE = await get_price_map(s)
    except Exception as e:  # pragma: no cover - defensivo
        logger.warning("pricing.refresh_cache.error", error=str(e))


# Tarifa de RESPALDO para un modelo que no está en la tabla, en dólares por
# millón de tokens. Es una estimación deliberada, no un precio real.
#
# POR QUÉ EXISTE. Contar 0 parece lo prudente ("no inventamos coste"), pero el
# gasto del mes es la SUMA de estos costes, y esa suma es lo que dispara el tope
# de presupuesto. Un modelo sin tarifa suma 0, así que el tope no salta nunca y
# el agente sigue respondiendo —y facturando— como si fuera gratis. Y para caer
# ahí basta con un identificador que no case literalmente: `gpt-5.4-mini` con la
# fecha detrás, el mismo con prefijo de proveedor, un ajuste fino, o cualquier
# modelo de OpenRouter que no estuviera en la semilla.
#
# Entre equivocarse por arriba (el tope salta antes de tiempo y alguien va a
# mirar por qué) y equivocarse por abajo (gasto sin freno y nadie se entera), se
# elige lo primero. El aviso de `llm.model_unpriced` sigue saliendo igual, y la
# solución de verdad es meter la tarifa buena en Precios de modelos.
PRECIO_RESPALDO = {"input": 1.0, "output": 5.0}


def modelo_sin_tarifa(
    model: str, prices: dict[str, dict[str, float]] | None = None
) -> bool:
    """True si de ese modelo no sabemos el precio (su coste es una estimación)."""
    table = prices if prices is not None else _PRICE_CACHE
    return bool(model) and model not in table


def estimate_cost_usd(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    prices: dict[str, dict[str, float]] | None = None,
) -> float:
    """Coste estimado en USD de una llamada concreta.

    `prices` opcional (mapa exacto al vuelo); si no, usa la caché del proceso.
    Si el modelo no está en el mapa se usa `PRECIO_RESPALDO`; ahí está explicado
    por qué no se devuelve 0.
    """
    table = prices if prices is not None else _PRICE_CACHE
    p = table.get(model) or (PRECIO_RESPALDO if model else None)
    if not p:
        return 0.0
    return (
        (prompt_tokens or 0) * p["input"] + (completion_tokens or 0) * p["output"]
    ) / 1_000_000
