"""Auto-actualización de precios de modelos desde OpenRouter.

OpenRouter expone un listado PÚBLICO de modelos con su precio por token en
`GET https://openrouter.ai/api/v1/models` (no requiere API key). Traemos ese
listado y actualizamos la tabla `llm_model_price` para los modelos que la app
usa de verdad, mapeando el nombre del modelo del app ↔ id de OpenRouter.

El mapeo es por sufijo: OpenRouter usa ids tipo `openai/gpt-5.4-mini` o
`z-ai/glm-5.2`; nosotros guardamos `gpt-5.4-mini` / `glm-5.2`. Casamos por la
parte posterior a la `/`. Lo que no case (p. ej. embeddings/moderation, que no
están en OpenRouter) se deja como esté (semilla o edición manual).

ANTHROPIC: EL SUFIJO NO BASTA
-----------------------------
Con Claude el casado por sufijo falla en silencio, y "falla en silencio" aquí
significa que el modelo cuenta 0 $ y el tope de presupuesto nunca salta.
Anthropic separa la versión con GUIONES en el id de su API y OpenRouter la
separa con un PUNTO:

    API de Anthropic        OpenRouter
    claude-opus-4-8    →    anthropic/claude-opus-4.8
    claude-sonnet-4-6  →    anthropic/claude-sonnet-4.6
    claude-haiku-4-5   →    anthropic/claude-haiku-4.5

`claude-opus-4-8` contra `claude-opus-4.8` no casa. Los modelos de una sola
cifra (`claude-opus-5`, `claude-sonnet-5`, `claude-fable-5`) sí casan tal cual.
Además, los ids fijados con fecha (`claude-haiku-4-5-20251001`) llevan un sufijo
`-AAAAMMDD` que OpenRouter no usa.

Por eso el casado prueba una LISTA DE CANDIDATOS por modelo
(`_match_candidates`) en vez de un único sufijo: el id tal cual, sin la fecha, y
con la versión en punto. Comprobado el 11-ago-2026 contra el listado real de
OpenRouter (403 modelos). Gemini no necesita traducción —sus ids ya llevan punto
en ambos lados (`gemini-2.5-flash`, `gemini-3.1-pro-preview`)— salvo por el
prefijo `models/` que devuelve el listado de Google.
"""
from __future__ import annotations

import os
import re

import httpx
from sqlalchemy import select, text as sql_text

from app.core.logging import get_logger
from app.models.llm_model_price import LLMModelPrice

logger = get_logger(__name__)

# Sufijo de fecha de los ids fijados de Anthropic: `...-4-5-20251001`.
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")
# Dos segmentos numéricos finales separados por guion: `...-4-8` → `...-4.8`.
_VERSION_RE = re.compile(r"-(\d+)-(\d+)$")

OPENROUTER_MODELS_URL = os.getenv(
    "OPENROUTER_MODELS_URL", "https://openrouter.ai/api/v1/models"
)


async def fetch_openrouter_prices() -> dict[str, dict[str, float]]:
    """Devuelve {openrouter_id: {"input_per_1m": x, "output_per_1m": y}}.

    Los precios de OpenRouter vienen en USD POR TOKEN (strings); los pasamos a
    USD por 1M de tokens. Lanza si la petición falla (el caller decide qué hacer).
    """
    headers = {}
    # La API key es opcional para el listado, pero si está la mandamos (por si
    # OpenRouter aplica rate limits distintos a usuarios autenticados).
    api_key = os.getenv("OPENROUTER_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(OPENROUTER_MODELS_URL, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    out: dict[str, dict[str, float]] = {}
    for m in data.get("data", []):
        mid = m.get("id")
        pricing = m.get("pricing") or {}
        if not mid:
            continue
        try:
            inp = float(pricing.get("prompt") or 0) * 1_000_000
            out_p = float(pricing.get("completion") or 0) * 1_000_000
        except (TypeError, ValueError):
            continue
        # Precios negativos = no disponible en OpenRouter; los ignoramos.
        if inp < 0 or out_p < 0:
            continue
        out[mid] = {"input_per_1m": round(inp, 6), "output_per_1m": round(out_p, 6)}
    return out


def _suffix(model_id: str) -> str:
    """Parte del id posterior a la última '/', en minúsculas."""
    return model_id.rsplit("/", 1)[-1].lower()


def _match_candidates(model: str) -> list[str]:
    """Claves con las que buscar `model` en el índice de OpenRouter, en orden.

    De más literal a más traducido, sin duplicados, para que un id que ya casa
    exactamente nunca se vea afectado por las reglas de traducción (los
    proveedores que ya funcionan siguen funcionando igual).

        gpt-5.4-mini              → [gpt-5.4-mini]
        claude-opus-4-8           → [claude-opus-4-8, claude-opus-4.8]
        claude-haiku-4-5-20251001 → [claude-haiku-4-5-20251001,
                                     claude-haiku-4-5, claude-haiku-4.5]
        models/gemini-2.5-flash   → [gemini-2.5-flash]
    """
    base = _suffix((model or "").strip())
    if not base:
        return []
    candidates = [base]

    def add(value: str) -> None:
        if value and value not in candidates:
            candidates.append(value)

    # Id fijado con fecha → el mismo sin la fecha.
    undated = _DATE_SUFFIX_RE.sub("", base)
    add(undated)
    # Versión con guiones → versión con punto (Anthropic ↔ OpenRouter). Se
    # aplica sobre el id YA sin fecha: hacerlo sobre el fechado convertiría
    # `claude-haiku-4-5-20251001` en `claude-haiku-4-5.20251001`, que no existe.
    add(_VERSION_RE.sub(r"-\1.\2", undated))
    return candidates


async def _app_models(db) -> set[str]:
    """Modelos que la app usa de verdad (para no traer las 300+ de OpenRouter).

    Union de: modelos vistos en el log de consumo + configurados en los agentes
    / clasificador / agente interno / config del agente público + los ya
    presentes en llm_model_price.
    """
    models: set[str] = set()
    queries = [
        "SELECT DISTINCT model FROM llm_usage_log WHERE model IS NOT NULL AND model <> ''",
        "SELECT DISTINCT model_name AS model FROM agents WHERE model_name IS NOT NULL AND model_name <> ''",
        "SELECT DISTINCT model_name AS model FROM classifier_config WHERE model_name IS NOT NULL AND model_name <> ''",
        "SELECT DISTINCT model_name AS model FROM internal_agent_config WHERE model_name IS NOT NULL AND model_name <> ''",
        "SELECT model FROM llm_model_price",
    ]
    for q in queries:
        try:
            rows = (await db.execute(sql_text(q))).fetchall()
            models.update((r.model or "").strip() for r in rows if (r.model or "").strip())
        except Exception as e:  # tabla ausente en algún entorno: no romper
            logger.warning("pricing.app_models.query_error", error=str(e))
    return models


async def refresh_model_prices(db, models: set[str] | None = None) -> dict:
    """Actualiza llm_model_price desde OpenRouter. Devuelve un resumen.

    No hace commit (lo hace el caller). Best-effort por modelo.
    """
    or_prices = await fetch_openrouter_prices()
    # Índice por sufijo → (id, precios). Si hay colisión de sufijo, gana el
    # primero (suele ser único para los modelos que usamos).
    by_suffix: dict[str, tuple[str, dict[str, float]]] = {}
    for mid, pr in or_prices.items():
        by_suffix.setdefault(_suffix(mid), (mid, pr))

    targets = models if models is not None else await _app_models(db)
    existing = {
        p.model: p for p in (await db.execute(select(LLMModelPrice))).scalars().all()
    }

    updated: list[str] = []
    unmatched: list[str] = []
    skipped_manual: list[str] = []
    for model in sorted(targets):
        # Respetamos las ediciones a mano: no las pisamos con OpenRouter.
        cur = existing.get(model)
        if cur is not None and cur.source == "manual":
            skipped_manual.append(model)
            continue
        match = next(
            (by_suffix[c] for c in _match_candidates(model) if c in by_suffix), None
        )
        if not match:
            unmatched.append(model)
            continue
        or_id, pr = match
        row = existing.get(model)
        if row is None:
            row = LLMModelPrice(model=model)
            db.add(row)
            existing[model] = row
        row.input_per_1m = pr["input_per_1m"]
        row.output_per_1m = pr["output_per_1m"]
        row.source = "openrouter"
        row.openrouter_id = or_id
        updated.append(model)

    await db.flush()
    logger.info(
        "pricing.refresh.done",
        updated=len(updated),
        unmatched=len(unmatched),
        skipped_manual=len(skipped_manual),
        total_openrouter=len(or_prices),
    )
    return {
        "updated": updated,
        "unmatched": unmatched,
        "skipped_manual": skipped_manual,
        "openrouter_models": len(or_prices),
    }
