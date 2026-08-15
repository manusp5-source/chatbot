"""Tests de precios de modelos (estimación de coste + auto-actualización).

- estimate_cost_usd: aritmética y tarifa de respaldo para modelos desconocidos.
- matching de OpenRouter: mapea nombre-del-app ↔ id de OpenRouter por sufijo.
- get_price_map (DB-gated): la BD gana sobre los defaults.
"""
from __future__ import annotations

import asyncio

import pytest


def test_estimate_cost_with_explicit_prices():
    from app.services.llm_pricing import estimate_cost_usd

    prices = {"glm-5.2": {"input": 0.6, "output": 2.2}}
    # 1M prompt * 0.6/1M + 1M completion * 2.2/1M = 2.8
    assert abs(estimate_cost_usd("glm-5.2", 1_000_000, 1_000_000, prices) - 2.8) < 1e-9
    # Tokens a 0 → 0.
    assert estimate_cost_usd("glm-5.2", 0, 0, prices) == 0.0


def test_un_modelo_sin_tarifa_no_sale_gratis():
    """Contar 0 dejaba el tope de presupuesto ciego: el gasto del mes es la suma
    de estos costes, y un modelo sin tarifa sumaba 0 para siempre."""
    from app.services.llm_pricing import (
        PRECIO_RESPALDO,
        estimate_cost_usd,
        modelo_sin_tarifa,
    )

    prices = {"glm-5.2": {"input": 0.6, "output": 2.2}}
    assert modelo_sin_tarifa("gpt-5.4-mini-2026-01-01", prices) is True
    assert modelo_sin_tarifa("glm-5.2", prices) is False

    coste = estimate_cost_usd("gpt-5.4-mini-2026-01-01", 1_000_000, 1_000_000, prices)
    esperado = PRECIO_RESPALDO["input"] + PRECIO_RESPALDO["output"]
    assert abs(coste - esperado) < 1e-9
    assert coste > 0, "el tope de presupuesto no saltaría nunca con este modelo"

    # Sin modelo no hay nada que estimar.
    assert estimate_cost_usd("", 1000, 1000, prices) == 0.0


def test_estimate_cost_uses_seed_cache_by_default():
    from app.services.llm_pricing import estimate_cost_usd

    # Sin pasar `prices`, usa la caché del proceso (arranca con los defaults).
    # gpt-5.4-mini: input 0.75, output 4.50 por 1M (tarifa pública de OpenAI;
    # la semilla los tenía a 0.15/0.60, cinco y siete veces y media por debajo).
    cost = estimate_cost_usd("gpt-5.4-mini", 1_000_000, 1_000_000)
    assert abs(cost - 5.25) < 1e-9


def test_openrouter_suffix_matching():
    """El matching por sufijo mapea el nombre del app al id de OpenRouter."""
    import app.services.openrouter_pricing as orp

    fake = {
        "openai/gpt-5.4-mini": {"input_per_1m": 0.15, "output_per_1m": 0.6},
        "z-ai/glm-5.2": {"input_per_1m": 0.5, "output_per_1m": 2.0},
    }

    async def fake_fetch():
        return fake

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return self._rows

        def fetchall(self):
            return self._rows

    class _Row:
        def __init__(self, model):
            self.model = model

    class _FakeDB:
        """Sesión mínima: _app_models via fetchall, existing via scalars().all()."""

        def __init__(self):
            self.added = []

        async def execute(self, q):
            text = str(q).lower()
            if "from llm_model_price" in text and "select model" in text:
                return _FakeResult([_Row("glm-5.2"), _Row("gpt-5.4-mini")])
            if "select" in text and "llm_model_price" in text:
                return _FakeResult([])  # existing rows (ORM select)
            return _FakeResult([])

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            pass

    orig = orp.fetch_openrouter_prices
    orp.fetch_openrouter_prices = fake_fetch
    try:
        db = _FakeDB()
        summary = asyncio.run(orp.refresh_model_prices(db, models={"glm-5.2", "gpt-5.4-mini", "text-embedding-3-small"}))
    finally:
        orp.fetch_openrouter_prices = orig

    assert set(summary["updated"]) == {"glm-5.2", "gpt-5.4-mini"}
    assert summary["unmatched"] == ["text-embedding-3-small"]  # no está en OpenRouter
    by_model = {p.model: p for p in db.added}
    assert by_model["glm-5.2"].openrouter_id == "z-ai/glm-5.2"
    assert float(by_model["glm-5.2"].input_per_1m) == 0.5
    assert by_model["glm-5.2"].source == "openrouter"


def _db_available() -> bool:
    try:
        from sqlalchemy import text

        from app.db.session import db_session

        async def _check():
            async with db_session() as db:
                await db.execute(text("select 1"))

        asyncio.run(_check())
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)
def test_get_price_map_db_overrides_seed():
    """get_price_map fusiona la BD sobre los defaults: la BD gana."""

    async def _run():
        from sqlalchemy import text

        from app.db.session import db_session
        from app.services.llm_pricing import get_price_map

        async with db_session() as db:
            # Fija un precio de BD distinto al default para gpt-5.4-mini.
            await db.execute(
                text(
                    "INSERT INTO llm_model_price (model, input_per_1m, output_per_1m, source) "
                    "VALUES ('gpt-5.4-mini', 9.99, 8.88, 'manual') "
                    "ON CONFLICT (model) DO UPDATE SET input_per_1m=9.99, output_per_1m=8.88, source='manual'"
                )
            )
            await db.commit()
            pm = await get_price_map(db)
            # limpieza: vuelve a dejarlo como semilla para no ensuciar otros
            # tests. Los valores son los de la tarifa pública de OpenAI (los
            # mismos que PRICE_PER_1M_USD); antes aquí se reescribían los
            # precios VIEJOS y erróneos, que es como se colaban de vuelta.
            await db.execute(
                text(
                    "UPDATE llm_model_price SET input_per_1m=0.75, output_per_1m=4.50, "
                    "source='seed' WHERE model='gpt-5.4-mini'"
                )
            )
            await db.commit()
            return pm

    pm = asyncio.run(_run())
    assert pm["gpt-5.4-mini"]["input"] == 9.99
    assert pm["gpt-5.4-mini"]["output"] == 8.88


def test_default_and_cache_are_independent():
    """Mutar la caché no debe corromper los defaults (dict copiado)."""
    from app.services import llm_pricing

    assert llm_pricing.PRICE_PER_1M_USD is not llm_pricing._PRICE_CACHE
