"""Tests del Agente Interno (unitarios, sin DB ni red).

Cobertura:
- Estructura: el paquete `app.agents.internal` no importa nada de los
  servicios de escritura del bot publico (auditoria contra "read-only").
- Tools: todos los schemas tienen el shape esperado por la API de OpenAI.
- Helpers: _mask_phone, _since_for.
- Budget: get_daily_count / incr_daily_count con un Redis fake.
"""
from __future__ import annotations

import importlib
import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


# --------------------- Estructura: nada de escrituras ---------------------


_FORBIDDEN_IMPORTS = {
    "app.services.conversation",  # store_incoming, escribe mensajes
    "app.services.agent_pause",  # set_*  — pero importamos solo getters
    "app.services.budget",  # bot publico, no aplicable
    "app.services.audit",  # auditoria la hace el endpoint, no el agente
}


def test_internal_package_does_not_import_writers():
    """Carga modulo a modulo y comprueba que los modulos del agente
    interno solo importan helpers de lectura. Toleramos `agent_pause`
    porque las tools usan SOLO sus getters (`is_agent_paused`,
    `get_channels_pause_state`, `list_demo_conversations`), no los setters.
    """
    # Filtramos los modulos a comprobar.
    targets = [
        "app.agents.internal.agent",
        "app.agents.internal.tools",
        "app.agents.internal.service",
        "app.agents.internal.budget",
    ]
    forbidden = _FORBIDDEN_IMPORTS - {"app.services.agent_pause"}  # ver docstring
    for modname in targets:
        # importlib.util permite leer el source sin ejecutar imports.
        spec = importlib.util.find_spec(modname)
        assert spec is not None and spec.origin is not None, f"missing {modname}"
        with open(spec.origin) as fp:
            src = fp.read()
        for bad in forbidden:
            assert bad not in src, (
                f"{modname} importa modulo de escritura {bad}: el agente interno debe ser read-only"
            )


def test_internal_tools_only_use_getters_of_agent_pause():
    """En tools.py solo permitimos getters de agent_pause."""
    spec = importlib.util.find_spec("app.agents.internal.tools")
    with open(spec.origin) as fp:
        src = fp.read()
    forbidden_calls = ["set_agent_paused", "set_channel_paused", "add_demo_conversation", "remove_demo_conversation"]
    for fn in forbidden_calls:
        assert fn not in src, f"tools.py llama a {fn} (setter): debe ser read-only"


# --------------------- Schemas de tools ---------------------


def test_all_tools_have_valid_schemas():
    from app.agents.internal.tools import ALL_TOOLS

    assert len(ALL_TOOLS) >= 5, "esperamos al menos 5 tools"
    for name, tool in ALL_TOOLS.items():
        assert tool.schema.name == name
        assert isinstance(tool.schema.description, str)
        assert len(tool.schema.description) > 20, f"description de {name} muy corta"
        params = tool.schema.parameters
        assert isinstance(params, dict)
        assert params.get("type") == "object"
        assert "properties" in params


def test_tools_expected_set_present():
    from app.agents.internal.tools import ALL_TOOLS

    expected = {
        "get_dashboard_stats",
        "list_conversations",
        "get_conversation_detail",
        "list_paused_channels",
        "search_contacts",
        "get_agent_usage",
        "get_handoff_summary",
        "get_message_volume",
    }
    assert expected.issubset(set(ALL_TOOLS.keys()))


# --------------------- Helpers ---------------------


def test_mask_phone_shapes():
    from app.agents.internal.tools import _mask_phone

    assert _mask_phone(None) == "(sin telefono)"
    assert _mask_phone("") == "(sin telefono)"
    assert _mask_phone("123") == "123"  # demasiado corto: passthrough
    masked = _mask_phone("+34911223344")
    # Asegura que no expongamos el numero entero pero si el contexto.
    assert masked.startswith("+34")
    assert masked.endswith("3344")
    assert "*" in masked
    assert "9112" not in masked  # los digitos centrales no deben aparecer


def test_since_for_ranges():
    from app.agents.internal.tools import _since_for

    today_since = _since_for("today")
    # Inicio del dia UTC.
    now = datetime.now(timezone.utc)
    assert today_since.tzinfo is not None
    assert today_since.year == now.year and today_since.month == now.month and today_since.day == now.day
    assert today_since.hour == 0 and today_since.minute == 0

    s7 = _since_for("7d")
    delta = datetime.now(timezone.utc) - s7
    assert timedelta(days=6, hours=23) <= delta <= timedelta(days=7, hours=1)

    # default si no reconoce
    s_unknown = _since_for("anything")
    delta_unknown = datetime.now(timezone.utc) - s_unknown
    assert timedelta(days=6, hours=23) <= delta_unknown <= timedelta(days=7, hours=1)


# --------------------- Budget rate limit (Redis fake) ---------------------


class _FakeRedisPipe:
    def __init__(self, store: dict):
        self.store = store
        self.ops: list = []

    def incr(self, key: str):
        self.ops.append(("incr", key))
        return self

    def expire(self, key: str, ttl: int):
        self.ops.append(("expire", key, ttl))
        return self

    async def execute(self):
        results = []
        for op in self.ops:
            if op[0] == "incr":
                k = op[1]
                self.store[k] = int(self.store.get(k, 0)) + 1
                results.append(self.store[k])
            elif op[0] == "expire":
                results.append(True)
        return results


class _FakeRedis:
    def __init__(self):
        self.store: dict = {}

    async def get(self, key: str):
        return str(self.store.get(key, "")).encode() if key in self.store else None

    def pipeline(self):
        return _FakeRedisPipe(self.store)


@pytest.mark.asyncio
async def test_incr_daily_count_increases_and_persists():
    from app.agents.internal import budget as b

    fake = _FakeRedis()
    user_id = uuid.uuid4()
    with patch.object(b, "get_redis", return_value=fake):
        assert await b.get_daily_count(user_id) == 0
        v1 = await b.incr_daily_count(user_id)
        v2 = await b.incr_daily_count(user_id)
        assert v1 == 1
        assert v2 == 2
        assert await b.get_daily_count(user_id) == 2


# --------------------- Config singleton ---------------------


def test_singleton_id_is_fixed():
    from app.models.internal_agent_config import InternalAgentConfig

    # El check constraint en BD impone que solo se admite este UUID.
    assert InternalAgentConfig.SINGLETON_ID == uuid.UUID(
        "00000000-0000-0000-0000-000000000001"
    )


# --------------------- Regresion: audit snapshot serializable ---------------------
#
# Bug real (produccion): al asignar un proveedor LLM al agente interno, el PUT
# devolvia "Network Error". Causa: el endpoint auditaba con
# `_cfg_to_out(cfg).model_dump()` (modo python), que deja `updated_at` como
# datetime y `llm_provider_id` como UUID. La columna audit_log.before/after es
# JSONB y se serializa con json.dumps → TypeError en el commit → 500 SIN
# cabeceras CORS → el navegador lo reporta como error de CORS, ocultando la
# causa. El fix: model_dump(mode="json") en el snapshot + json_serializer con
# default=str en el engine.


def test_internal_agent_config_out_json_dump_is_serializable():
    """El snapshot de auditoria (mode='json') debe pasar por json.dumps SIN
    default, incluso con un proveedor asignado (UUID) y updated_at (datetime).
    """
    import json
    from datetime import datetime, timezone

    from app.api.admin import InternalAgentConfigOut

    out = InternalAgentConfigOut(
        model_name="glm-5.2",
        temperature=0.2,
        max_tokens=1500,
        monthly_budget_usd=None,
        daily_query_limit=50,
        system_prompt="hola",
        llm_provider_id=uuid.uuid4(),  # proveedor asignado → UUID crudo en modo python
        updated_at=datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc),
    )
    snapshot = out.model_dump(mode="json")
    # No debe lanzar (sin argumento default): todo es JSON-nativo.
    dumped = json.dumps(snapshot)
    assert isinstance(dumped, str)
    assert isinstance(snapshot["llm_provider_id"], str)
    assert isinstance(snapshot["updated_at"], str)


def _db_available() -> bool:
    try:
        from sqlalchemy import text

        from app.db.session import db_session

        async def _check():
            async with db_session() as db:
                await db.execute(text("select 1"))

        import asyncio as _asyncio

        _asyncio.run(_check())
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)
def test_update_config_no_missing_greenlet():
    """Regresion (DB-gated): tras update_config, leer los atributos del cfg en
    contexto SINCRONO (lo que hace _cfg_to_out al construir respuesta/auditoria)
    NO debe lanzar MissingGreenlet.

    `updated_at` usa onupdate=func.now(): el flush lo deja EXPIRADO en la
    instancia, y leerlo dispararia un SELECT perezoso fuera del greenlet async.
    update_config hace `await db.refresh(cfg)` para evitarlo. Sin ese refresh,
    esta prueba peta con sqlalchemy.exc.MissingGreenlet.
    """
    import asyncio

    async def _run():
        from app.agents.internal.service import get_config, update_config

        from app.db.session import db_session

        async with db_session() as db:
            await get_config(db)  # asegura el singleton (seed de la migracion 0012)
            cfg = await update_config(db, model_name="glm-5.2", temperature=0.2, updated_by=None)
            # Lecturas SINCRONAS, como _cfg_to_out. El fallo (si lo hubiera)
            # ocurre AQUI, al tocar updated_at expirado.
            snapshot = (
                cfg.model_name,
                float(cfg.temperature),
                cfg.updated_at,
                cfg.llm_provider_id,
            )
            await db.rollback()  # no persistimos el cambio de prueba
            return snapshot

    model_name, temperature, updated_at, _ = asyncio.run(_run())
    assert model_name == "glm-5.2"
    assert temperature == 0.2
    assert updated_at is not None


def test_db_json_serializer_tolerates_datetime_uuid_decimal():
    """Red de seguridad a nivel de engine: cualquier escritura JSONB con
    datetime/UUID/Decimal no debe reventar (default=str)."""
    import json
    from datetime import datetime, timezone
    from decimal import Decimal

    from app.db.session import _json_serializer

    payload = {
        "when": datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc),
        "provider": uuid.uuid4(),
        "cost": Decimal("1.23"),
        "plain": "ok",
        "n": 3,
    }
    dumped = _json_serializer(payload)
    # Round-trip valido y sin perdida de las claves.
    parsed = json.loads(dumped)
    assert parsed["plain"] == "ok"
    assert parsed["n"] == 3
    assert isinstance(parsed["when"], str)
    assert isinstance(parsed["provider"], str)
